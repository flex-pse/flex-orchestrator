"""Synthetic data the tutorials share, so each notebook stays about the orchestrator."""

from pathlib import Path

import numpy as np
import pandas as pd

ASSETS = Path(__file__).parent / "assets"


def metered_demand() -> pd.DataFrame:
    """A pump's metered flow in January and July 2025, every 15 minutes, in m^3/hr.

    A daily cycle around 25 m^3/hr in winter and 35 in summer, plus noise.
    """
    rng = np.random.default_rng(0)
    months = [
        pd.date_range(f"2025-{month:02d}-01", periods=31 * 96, freq="15min", tz="UTC")
        for month in (1, 7)
    ]
    time = months[0].append(months[1])
    base = np.where(time.month == 7, 35.0, 25.0)
    daily = 8 * np.sin(2 * np.pi * (time.hour + time.minute / 60 - 8) / 24)
    flow = base + daily + rng.normal(0, 1, len(time))
    return pd.DataFrame({"flow": flow}, time)


def projection(metered: pd.DataFrame, year: int, growth: float) -> pd.DataFrame:
    """The 2025 data moved to ``year``, grown by ``growth`` every five years."""
    projected = metered * (1 + growth) ** ((year - 2025) / 5)
    projected.index = metered.index + pd.DateOffset(years=year - 2025)
    return projected


def noisy(projected: pd.DataFrame, seed: int, level: float) -> pd.DataFrame:
    """``projected`` with random swings: hourly by ``level``, daily by half that."""
    rng = np.random.default_rng(seed)
    hours = projected.index.floor("h")
    days = projected.index.floor("D")
    hourly = pd.Series(rng.normal(0, level, hours.nunique()), hours.unique())
    daily = pd.Series(rng.normal(0, level / 2, days.nunique()), days.unique())
    factor = 1 + hourly[hours].to_numpy() + daily[days].to_numpy()
    return projected.mul(factor, axis=0)


def naive(index: pd.DatetimeIndex) -> pd.DatetimeIndex:
    """``index`` in UTC without its timezone, which reads cleaner on a chart axis."""
    return index.tz_convert("UTC").tz_localize(None) if index.tz else index


# The RO plant of tutorials 1 to 4: feed -> RO -> tank -> product, which must
# deliver a constant 90 m^3/hr while buying electricity on a summer
# time-of-use tariff (assets/ro_tariff.csv). The tank lets the RO run harder
# off-peak. The tank is cyclic: it must end the two days at least as full
# as it starts, so the model can't run it down for free.

RO_UNITS = {"permeate": "m^3/hr", "power_electrical": "kW"}
RO_TARIFF = ASSETS / "ro_tariff.csv"


def sec(permeate):
    """The RO's true specific energy (kWh/m^3), lowest at 90 m^3/hr."""
    return 2.5 + 4e-4 * (permeate - 90) ** 2


def operating_data(noise: float = 0.02, seed: int = 0) -> pd.DataFrame:
    """A month (June 2025) of hourly RO data: power = sec(permeate) * permeate.

    The permeate is spread over 30 to 150 m^3/hr; the power has ``noise``
    (relative) on it.
    """
    rng = np.random.default_rng(seed)
    time = pd.date_range("2025-06-01", periods=30 * 24, freq="h", tz="UTC")
    permeate = rng.uniform(30, 150, len(time))
    power = sec(permeate) * permeate * rng.normal(1, noise, len(time))
    return pd.DataFrame({"permeate": permeate, "power_electrical": power}, time)


def ro_model(tank_volume: float = 1500.0):
    """The RO plant, built in Python with flex-pse, for 8 to 10 July 2025."""
    import flexops as fo
    import pyomo.environ as pyo
    from pyomo.environ import units as u
    from pyomo.network import Arc

    flow = u.m**3 / u.hr
    m = pyo.ConcreteModel(name="ro_plant")
    m.time_block = fo.TimeBlock(
        start_date="2025-07-08", end_date="2025-07-10", time_step=1 * u.hr
    )
    m.properties = fo.SimpleAqueousFlow()
    m.costing = fo.FlexCosting(time_block=m.time_block, tariff_file=str(RO_TARIFF))
    m.plant = fo.PlantBlock(time_block=m.time_block)
    m.plant.feed = fo.Feed(property_package=m.properties, max_withdrawal=300 * flow)
    m.plant.ro = fo.ReverseOsmosis(
        property_package=m.properties,
        costing_package=m.costing,
        recovery=0.5,
        energy_intensity=3.0 * u.kWh / u.m**3,
    )
    m.plant.tank = fo.Tank(
        property_package=m.properties,
        max_volume=tank_volume * u.m**3,
        initial_volume=tank_volume / 2 * u.m**3,
        cyclic=True,  # ends the horizon at least as full as it starts
    )
    m.plant.product = fo.Product(
        property_package=m.properties, min_demand=90 * flow, max_demand=90 * flow
    )
    m.plant.brine = fo.Product(property_package=m.properties)
    m.plant.feed_to_ro = Arc(source=m.plant.feed.outlet_a, destination=m.plant.ro.inlet)
    m.plant.ro_to_tank = Arc(source=m.plant.ro.outlet_a, destination=m.plant.tank.inlet)
    m.plant.ro_to_brine = Arc(
        source=m.plant.ro.outlet_b, destination=m.plant.brine.inlet_a
    )
    m.plant.tank_to_product = Arc(
        source=m.plant.tank.outlet, destination=m.plant.product.inlet_a
    )
    pyo.TransformationFactory("network.expand_arcs").apply_to(m)
    m.costing.cost_process()
    m.objective = pyo.Objective(expr=m.costing.aggregate_operating_cost)
    return m


def ro_spec(tank_volume: float = 1500.0) -> dict:
    """The RO plant's flowsheet spec, emitted by flex-pse from :func:`ro_model`."""
    import flexops as fo

    return fo.emit_model(ro_model(tank_volume), relative_to=ASSETS)


def energy_price(index: pd.DatetimeIndex) -> pd.Series:
    """The tariff's energy price ($/kWh) at each time: 0.18 on summer weekday
    afternoons (16:00 to 21:00, June to September), 0.09 otherwise.

    The tariff also has demand charges ($/kW of the month's peak), which the
    model costs too; this is only the per-kWh part, for charts.
    """
    peak = (
        index.month.isin([6, 7, 8, 9])
        & (index.dayofweek < 5)
        & (index.hour >= 16)
        & (index.hour < 21)
    )
    return pd.Series(np.where(peak, 0.18, 0.09), index, name="price")
