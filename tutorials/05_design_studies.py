import marimo

__generated_with = "0.25.2"
app = marimo.App(width="medium", app_title="Design studies")


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""
    # Design studies

    A **design study** sizes equipment, here a battery and solar panels, so
    that one plant runs at the lowest cost over many years. Solving every hour
    of ten years is too big, so the study models a few **horizons**: a
    modeled year and a representative month, each a copy of the plant with
    its own time steps. One sizing is shared by all of them.

    This tutorial:

    1. lays out the horizons;
    2. projects a pump's demand onto each modeled year;
    3. sets each horizon's demand from its own year's projection, and shows
       how the data reached the model;
    4. sizes the plant;
    5. sweeps over demand scenarios: the projection, and noisy draws of it;
    6. rebuilds one solved model, to read any variable of it.

    Move the sliders in section 2 and everything below re-runs.
    """)
    return


@app.cell
def _():
    import tempfile

    import altair as alt
    import marimo as mo
    import pandas as pd
    import pyomo.environ as pyo
    import yaml
    from tutorial_data import ASSETS, metered_demand, naive, noisy, projection

    from flexorchestrator import Orchestrator
    from flexorchestrator.backend import FileBackend
    from flexorchestrator.flexpse import solve

    return (
        ASSETS,
        FileBackend,
        Orchestrator,
        alt,
        metered_demand,
        mo,
        naive,
        noisy,
        pd,
        projection,
        pyo,
        solve,
        tempfile,
        yaml,
    )


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""
    ## 1. Horizons

    A study config's `design:` section says which years and months to model,
    and what to size. Each year stands in for `represents` years (2030 for
    2030 to 2034), each month for `weight` months of the year, and `days: 1`
    models one day of each month, to keep this tutorial quick. `changes`
    vary the plant between horizons: electricity costs more from 2035, and
    the sun shines longer in July.
    """)
    return


@app.cell
def _(mo, yaml):
    design_yaml = """
    years:
      - {year: 2030, represents: 5}
      - {year: 2035, represents: 5}
    months:
      - {month: 1, weight: 6, days: 1}
      - {month: 7, weight: 6, days: 1}
    changes:
      - element: costing
        years: [2035]
        values: {energy_prices: {electrical: {value: 0.15, units: USD/kWh}}}
      - element: pv
        months: [7]
        values:
          construction_options:
            capacity_factor: [0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1,
                              1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0]
    capacities:
      - name: battery_energy
        variable: battery.energy_capacity
        capital_cost: 300
        lifetime_years: 15
        upper: 1500
      - name: solar
        variable: pv.capacity
        capital_cost: 600
        lifetime_years: 25
        upper: 1500
    """
    design = yaml.safe_load(design_yaml)
    mo.md(f"```yaml\ndesign:{design_yaml.rstrip()}\n```")
    return (design,)


@app.cell
def _(alt, design, mo, pd):
    _rows = [
        {
            "year": str(y["year"]),
            "month": [
                "Jan",
                "Feb",
                "Mar",
                "Apr",
                "May",
                "Jun",
                "Jul",
                "Aug",
                "Sep",
                "Oct",
                "Nov",
                "Dec",
            ][m["month"] - 1],
            "horizon": f"horizon[{y['year']},{m['month']}]",
            "stands for": f"{m['weight']} months x {y['represents']} years",
        }
        for y in design["years"]
        for m in design["months"]
    ]
    _base = alt.Chart(pd.DataFrame(_rows)).encode(
        x=alt.X("month:N", title=None, sort=None),
        y=alt.Y("year:N", title=None),
    )
    _grid = _base.mark_rect(cornerRadius=6, opacity=0.25, color="#4c78a8").encode(
        tooltip=["horizon", "stands for"]
    ) + _base.mark_text(fontSize=13).encode(text="horizon")
    mo.vstack(
        [
            _grid.properties(width=420, height=140, title="The study's horizons"),
            mo.md(
                "Each horizon is a block of the merged model, named "
                "`horizon[<year>,<month>]`. A path such as "
                "`horizon[2030,7].battery.charge` names a variable in one of them."
            ),
        ]
    )
    return


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""
    ## 2. Demand data per modeled year

    The pump's demand is metered in 2025, every 15 minutes. The modeled years
    are in the future, so we project it: the 2025 data moved to each modeled
    year and grown by a rate every five years. Each projection is stored as
    its own dataset, `projection_2030` and `projection_2035`.
    """)
    return


@app.cell
def _(mo):
    growth = mo.ui.slider(0, 30, step=5, value=10, label="Growth every 5 years (%)")
    noise = mo.ui.slider(0, 20, step=5, value=10, label="Scenario noise (%)")
    mo.hstack([growth, noise], justify="start", gap=2)
    return growth, noise


@app.cell
def _(growth, metered_demand, projection):
    metered = metered_demand()
    projections = {
        year: projection(metered, year, growth.value / 100) for year in (2030, 2035)
    }
    return metered, projections


@app.cell
def _(alt, metered, naive, pd, projections):
    def first_day(frame, label):
        """The first day of each month of ``frame``, by hour of day."""
        day = frame[frame.index.day == 1]
        return pd.DataFrame(
            {
                "hour": day.index.hour + day.index.minute / 60,
                "month": day.index.strftime("%B"),
                "flow": day["flow"].to_numpy(),
                "data": label,
                "time": naive(day.index),
            }
        )

    _days = pd.concat(
        [first_day(metered, "metered 2025")]
        + [
            first_day(frame, f"projection_{year}")
            for year, frame in projections.items()
        ]
    )
    alt.Chart(_days).mark_line().encode(
        x=alt.X("hour:Q", title="Hour of day", scale=alt.Scale(domain=[0, 24])),
        y=alt.Y("flow:Q", title="Pump demand (m³/hr)"),
        color=alt.Color("data:N", title=None),
        strokeDash=alt.condition(
            alt.datum.data == "metered 2025", alt.value([4, 3]), alt.value([1, 0])
        ),
        column=alt.Column("month:N", title=None),
        tooltip=["data", "time", alt.Tooltip("flow:Q", format=".1f")],
    ).properties(width=300, height=220, title="The first day of each month")
    return


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""
    ## 3. Store everything and run the study

    The orchestrator stores in a folder here (`FileBackend`); with Acquirium,
    only the `Orchestrator(...)` line changes. We store the plant's flowsheet
    as a model spec, and the data as datasets.
    """)
    return


@app.cell
def _(ASSETS, FileBackend, Orchestrator, metered, projections, tempfile):
    import json

    orch = Orchestrator(
        FileBackend(tempfile.mkdtemp(prefix="tutorial-")), frame="pandas"
    )
    orch.create_study("sizing")
    orch.store_model_spec(
        "plant", json.loads((ASSETS / "design_flowsheet.json").read_text())
    )
    orch.store_data("metered_demand", metered, units={"flow": "m^3/hr"})
    for _year, _frame in projections.items():
        orch.store_data(f"projection_{_year}", _frame, units={"flow": "m^3/hr"})
    return (orch,)


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""
    `model_inputs` set model variables from stored data. `rename` maps a
    dataset column to the variable path it sets, and the path picks the
    horizons:

    | Path | Sets |
    | --- | --- |
    | `pump.inlet_state...` (a path in the flowsheet) | every horizon |
    | `horizon[2030,7].pump.inlet_state...` | July 2030 only |
    | `horizon[2030,*].pump.inlet_state...` | every 2030 horizon |
    | `horizon[*,7].pump.inlet_state...` | every July |

    So each year takes its own projection with a `*` for the month. The `*` in
    `flow_vol_phase[*,Liq]` is the variable's own time index, not a horizon.
    """)
    return


@app.cell
def _(design, mo, yaml):
    inputs_yaml = """
    - dataset: projection_2030
      columns: [flow]
      rename: {flow: "horizon[2030,*].pump.inlet_state.flow_vol_phase[*,Liq]"}
      resample: {agg: mean}  # the 15-minute samples in each hour, averaged
    - dataset: projection_2035
      columns: [flow]
      rename: {flow: "horizon[2035,*].pump.inlet_state.flow_vol_phase[*,Liq]"}
      resample: {agg: mean}
    """
    series = ["pump.power_electrical", "pv.power_generated", "battery.charge"]
    study_config = {
        "base_model_spec": "plant",
        "design": design,
        "model_inputs": yaml.safe_load(inputs_yaml),
        "output_name": "sized",
        # The design summary (sizes and costs), plus a few series per horizon.
        "export": {
            "include": [
                "capacity*",
                "annual*",
                "horizon*.costing.aggregate_operating_cost",
                *(f"horizon*.{name}" for name in series),
            ]
        },
    }
    mo.md(f"```yaml\nmodel_inputs:{inputs_yaml.rstrip()}\n```")
    return series, study_config


@app.cell
def _(mo, orch, pd, study_config):
    run = orch.execute_study(study_config)
    summary = orch.design_results("sized")
    sizes = pd.DataFrame(
        [
            {
                "capacity": name,
                "size": round(capacity.size, 1),
                "units": capacity.units,
                "annualized cost": round(capacity.annualized_capital_cost),
            }
            for name, capacity in summary.capacities.items()
        ]
    )
    mo.vstack(
        [
            mo.md(
                f"**{run.counts()}**: the plant costs "
                f"**{summary.annualized_cost:,.0f} {summary.currency} per year**, "
                f"{summary.annualized_operating_cost:,.0f} of it to operate."
            ),
            mo.ui.table(sizes, selection=None),
        ]
    )
    return


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""
    ## 4. How the data reached the model

    Each horizon read its own dates of its own year's projection, and the
    15-minute data was lined up with the horizon's hourly time steps: the four
    samples in each hour, averaged (`resample: {agg: mean}`). The result
    records where every value came from (`result.model_inputs`) and the
    values fixed (`result.overrides`). *Tutorial 2* covers the other ways to
    line data up: `agg`, `window` (forward or backward), `gaps` and `reduce`.
    """)
    return


@app.cell
def _(mo, orch, pd):
    result = orch.result("sized")
    provenance = pd.DataFrame(
        [
            {
                "horizon": source["scope"],
                "dataset": source["resolved"]["dataset"],
                "window start": source["window"]["start"],
                "window end": source["window"]["end"],
                "steps filled": sum(source["filled"].values()),
            }
            for source in result.model_inputs
        ]
    )
    mo.ui.table(provenance, selection=None)
    return (result,)


@app.cell
def _(alt, naive, orch, pd, result):
    model_values = orch.model_results("sized")
    _raw, _fixed = [], []
    for _source in result.model_inputs:
        _scope = _source["scope"]
        _window = _source["window"]
        _data = orch.get_data(
            _source["resolved"]["dataset"],
            start=pd.Timestamp(_window["start"]),
            end=pd.Timestamp(_window["end"]),
        )
        _raw.append(
            pd.DataFrame(
                {"time": naive(_data.index), "flow": _data["flow"], "horizon": _scope}
            )
        )
        [_path] = _source["paths"]
        _steps = model_values[f"{_scope}.pump.power_electrical"].timestamps
        _fixed.append(
            pd.DataFrame(
                {
                    "time": naive(pd.DatetimeIndex(_steps)),
                    "flow": result.overrides[_path]["value"],
                    "horizon": _scope,
                }
            )
        )
    _x = alt.X("hoursminutes(time):T", title="Hour of day")
    _y = alt.Y("flow:Q", title="Pump demand (m³/hr)")
    _data_line = alt.Chart().mark_line(color="#bbb").encode(x=_x, y=_y)
    _model_steps = (
        alt.Chart()
        .mark_line(interpolate="step-after", color="#e45756", strokeWidth=2)
        .encode(x=_x, y=_y)
    )
    _layers = alt.layer(
        _data_line.transform_filter(alt.datum.kind == "data"),
        _model_steps.transform_filter(alt.datum.kind == "model"),
        data=pd.concat(
            [
                pd.concat(_raw).assign(kind="data"),
                pd.concat(_fixed).assign(kind="model"),
            ]
        ),
    )
    _layers.properties(width=260, height=160).facet(
        facet=alt.Facet("horizon:N", title=None), columns=2
    ).properties(title="The stored data (grey) and the values fixed on the model (red)")
    return (model_values,)


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""
    ## The sized plant, per horizon

    The pump's power, the solar output and the battery's charge in each
    horizon. These series were stored because the config's `export` asked
    for them.
    """)
    return


@app.cell
def _(alt, model_values, naive, pd, result, series):
    _frames = []
    for _source in result.model_inputs:
        for _name in series:
            _s = model_values[f"{_source['scope']}.{_name}"]
            _frames.append(
                pd.DataFrame(
                    {
                        "time": naive(pd.DatetimeIndex(_s.timestamps)),
                        "value": _s.values,
                        "series": f"{_name} ({_s.unit_pyomo})",
                        "horizon": _source["scope"],
                    }
                )
            )
    alt.Chart(pd.concat(_frames)).mark_line().encode(
        x=alt.X("hoursminutes(time):T", title="Hour of day"),
        y=alt.Y("value:Q", title=None),
        color=alt.Color("series:N", title=None, legend=alt.Legend(orient="top")),
    ).properties(width=260, height=160).facet(
        facet=alt.Facet("horizon:N", title=None), columns=2
    )
    return


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""
    ## 5. Sweeping over demand scenarios

    A projection is one guess. To see how the sizes depend on it, store
    other scenarios, here the projection with random hourly and daily swings
    (the noise slider in section 2), two draws of it. A `model_inputs` sweep
    gives each task one scenario: each value is a labelled set of
    `model_inputs` entries.
    """)
    return


@app.cell
def _(mo, noise, noisy, orch, projections, yaml):
    _units = {"flow": "m^3/hr"}
    for _year, _frame in projections.items():
        for _seed in (1, 2):
            orch.store_data(
                f"noisy_{_seed}_{_year}",
                noisy(_frame, _seed, noise.value / 100),
                units=_units,
            )

    demand = "pump.inlet_state.flow_vol_phase[*,Liq]"

    def scenario(label, prefix):
        """A sweep value: each year's dataset, named ``<prefix>_<year>``."""
        return {
            "label": label,
            "inputs": [
                {
                    "dataset": f"{prefix}_{year}",
                    "columns": ["flow"],
                    "rename": {"flow": f"horizon[{year},*].{demand}"},
                }
                for year in (2030, 2035)
            ],
        }

    scenario_sweep = {
        "type": "model_inputs",
        "name": "scenario",
        "values": [
            scenario("growth", "projection"),
            scenario("noisy_1", "noisy_1"),
            scenario("noisy_2", "noisy_2"),
        ],
    }
    mo.md(
        "```yaml\nsweeps:\n"
        + yaml.safe_dump(
            [scenario_sweep], sort_keys=False, width=100, default_flow_style=None
        )
        + "output_name: '{scenario}'\n```"
    )
    return (scenario_sweep,)


@app.cell
def _(orch, scenario_sweep, study_config):
    _config = {
        **study_config,
        "model_inputs": [],
        "sweeps": [scenario_sweep],
        "output_name": "{scenario}",
    }
    scenario_run = orch.execute_study(_config)
    return (scenario_run,)


@app.cell
def _(alt, mo, orch, pd, scenario_run):
    _rows = []
    for _task in scenario_run.tasks:
        _summary = orch.design_results(_task.tag)
        for _name, _capacity in _summary.capacities.items():
            _rows.append(
                {
                    "scenario": _task.tag,
                    "capacity": f"{_name} ({_capacity.units})",
                    "size": _capacity.size,
                }
            )
        _rows.append(
            {
                "scenario": _task.tag,
                "capacity": "annualized cost (USD)",
                "size": _summary.annualized_cost,
            }
        )
    _chart = (
        alt.Chart(pd.DataFrame(_rows))
        .mark_bar()
        .encode(
            x=alt.X("scenario:N", title=None),
            y=alt.Y("size:Q", title=None),
            color=alt.Color("scenario:N", legend=None),
            column=alt.Column("capacity:N", title=None),
            tooltip=["scenario", alt.Tooltip("size:Q", format=",.1f")],
        )
        .resolve_scale(y="independent")
        .properties(width=150, height=200)
    )
    mo.vstack([mo.md(f"**{scenario_run.counts()}**"), _chart])
    return


@app.cell
def _(alt, naive, orch, pd, scenario_run):
    _frames = []
    for _task in scenario_run.tasks:
        _result = orch.result(_task.tag)
        for _source in _result.model_inputs:
            if _source["scope"] != "horizon[2030,7]":
                continue
            [_path] = _source["paths"]
            _steps = orch.model_results(_task.tag)[
                "horizon[2030,7].pump.power_electrical"
            ].timestamps
            _frames.append(
                pd.DataFrame(
                    {
                        "time": naive(pd.DatetimeIndex(_steps)),
                        "flow": _result.overrides[_path]["value"],
                        "scenario": _task.tag,
                    }
                )
            )
    alt.Chart(pd.concat(_frames)).mark_line(interpolate="step-after").encode(
        x=alt.X("hoursminutes(time):T", title="Hour of day"),
        y=alt.Y("flow:Q", title="Pump demand (m³/hr)"),
        color=alt.Color("scenario:N", title=None),
    ).properties(
        width=560, height=220, title="July 2030 demand, as each scenario set it"
    )
    return


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""
    ## 6. Getting a solved model back

    A study stores values, not the Pyomo model: its tasks may run in other
    processes, or on Ray. Each result records its spec and every value it
    fixed, the scenario's demand included, so the model can be rebuilt and
    solved again, to read any variable of it, exported or not.

    ```python
    growth = orch.result("growth")
    model = orch.build(growth.spec_tag, **growth.overrides)
    solve(model)
    ```
    """)
    return


@app.cell
def _(mo, orch, pyo, solve):
    growth_result = orch.result("growth")
    model = orch.build(growth_result.spec_tag, **growth_result.overrides)
    outcome = solve(model)
    _july = model.horizon[2030, 7]
    mo.md(
        f"Solve: **{outcome.status}**. The rebuilt battery holds "
        f"**{pyo.value(_july.battery.energy_capacity):.1f} kWh**, as the sweep found."
    )
    return (model,)


@app.cell
def _(alt, model, pd, pyo):
    _july = model.horizon[2030, 7]
    # The time block's steps (0, 1, ... 23) and the datetime each one starts at.
    _steps = list(_july.time_block.time_index)
    _when = _july.time_block.datetime_index
    _hour = pd.Timedelta(hours=1)
    # Power over each hour, drawn at the hour's middle.
    _power_data = pd.DataFrame(
        {
            "time": _when + _hour / 2,
            "charging": [pyo.value(_july.battery.power_charge[t]) for t in _steps],
            "discharging": [
                -pyo.value(_july.battery.power_discharge[t]) for t in _steps
            ],
        }
    ).melt("time", ["charging", "discharging"], "battery", "kW")
    # charge[t] is what the battery holds at the end of step t; charge_start
    # what it holds at 00:00, which the solve chooses (fix_initial_state is
    # off) and the cyclic constraint keeps at or below the day's last charge.
    _levels = [pyo.value(_july.battery.charge[t]) for t in _steps]
    _start = pyo.value(_july.battery.charge_start)
    _stored_data = pd.DataFrame(
        {"time": [_when[0], *(_when + _hour)], "stored": [_start, *_levels]}
    )
    _x = alt.X("time:T", title="2030-07-01", axis=alt.Axis(format="%H:%M"))
    # One color scale for both layers, so the line gets a legend entry too.
    _color = alt.Color(
        "battery:N",
        title=None,
        scale=alt.Scale(
            domain=["charging", "discharging", "stored energy"],
            range=["#4c78a8", "#f58518", "#54a24b"],
        ),
    )
    _power = (
        alt.Chart(_power_data)
        .mark_bar(width=14)
        .encode(x=_x, y=alt.Y("kW:Q", title="Battery power (kW)"), color=_color)
    )
    _stored = (
        alt.Chart(_stored_data.assign(battery="stored energy"))
        .mark_line(strokeWidth=3, point=alt.OverlayMarkDef(size=40, filled=True))
        .encode(x=_x, y=alt.Y("stored:Q", title="Stored energy (kWh)"), color=_color)
    )
    (_power + _stored).resolve_scale(y="independent").properties(
        width=560,
        height=240,
        title="July 2030 battery, read from the rebuilt model (not exported)",
    )
    return


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""
    Each horizon models one day: 24 hourly steps, from 00:00 to midnight. The
    bars are the power over each hour; the line is what the battery holds at
    each hour's end. Storage is **cyclic**: the battery must end the day
    holding at least what it started with, because the day stands for every
    day of its month (`cyclic: true`). How much it starts with is the solve's
    choice (`fix_initial_state: false`; a design study refuses a fixed one).
    So the day starts with energy left from the evening before,
    which runs the pump until about 02:00; the battery charges from 08:00
    while the sun is up, and runs the pump again from 18:00.
    """)
    return


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""
    ## Where next

    - The docs' **Studies from a config** page covers every way to name
      horizons and supply their data, including reusing past data as it is
      with `data_years`.
    - Add another sweep next to `scenario`, such as the solar price
      (`capacities.solar.capital_cost`), and every scenario runs at every
      price.
    """)
    return


if __name__ == "__main__":
    app.run()
