"""Size equipment under several demand scenarios, swept as model inputs.

Each scenario is a projection of the pump's demand per modeled year, stored as
a dataset each: ``growth`` is the projection example.py uses, and ``noisy_1``
and ``noisy_2`` add random variation to it, a draw each. scenarios.yaml sweeps
over them, so each task sizes the plant for one scenario. Then it rebuilds
the ``growth`` model from its stored result and solves it again, to work with
the Pyomo model itself.

    python examples/design_study/sweep_scenarios.py
"""

import json
import os
import shutil
from pathlib import Path

import numpy as np
import pandas as pd
import pyomo.environ as pyo
from example import metered_demand, projection

from flexorchestrator import Orchestrator
from flexorchestrator.backend import FileBackend
from flexorchestrator.flexpse import solve

HERE = Path(__file__).parent
FOLDER = HERE / "on_disk_scenarios"
YEARS = (2030, 2035)


def noisy(projected: pd.DataFrame, seed: int) -> pd.DataFrame:
    """``projected`` with random swings: hourly +-10%, and a day-to-day drift."""
    rng = np.random.default_rng(seed)
    hours = projected.index.floor("h")
    days = projected.index.floor("D")
    hourly = pd.Series(rng.normal(0, 0.10, hours.nunique()), hours.unique())
    daily = pd.Series(rng.normal(0, 0.05, days.nunique()), days.unique())
    factor = 1 + hourly[hours].to_numpy() + daily[days].to_numpy()
    return projected.mul(factor, axis=0)


if __name__ == "__main__":
    os.chdir(HERE)  # the config names sun_july.json relative to here
    shutil.rmtree(FOLDER, ignore_errors=True)  # start afresh each time
    with Orchestrator(FileBackend(FOLDER)) as orch:
        orch.create_study("scenarios")
        orch.store_model_spec("plant", json.loads(Path("flowsheet.json").read_text()))
        metered = metered_demand()
        units = {"flow": "m^3/hr"}
        for year in YEARS:  # each scenario stores one dataset per modeled year
            projected = projection(metered, year)
            orch.store_data(f"projection_{year}", projected, units=units)
            for seed in (1, 2):
                orch.store_data(
                    f"noisy_{seed}_{year}", noisy(projected, seed), units=units
                )
        run = orch.execute_study("scenarios.yaml")
        print(run.counts())
        for task in run.tasks:
            summary = orch.design_results(task.tag)
            sizes = {
                name: f"{capacity.size:.1f} {capacity.units}"
                for name, capacity in summary.capacities.items()
            }
            print(f"{task.tag}: {sizes}, {summary.annualized_cost:,.0f} per year")

        # Every result records the datasets its horizons read.
        for task in run.tasks:
            read = sorted(
                {s["resolved"]["dataset"] for s in orch.result(task.tag).model_inputs}
            )
            print(f"{task.tag} read {', '.join(read)}")

        # The study stores values, not the Pyomo model. To get the model back,
        # rebuild it: the result records its spec and every value it fixed,
        # including the demand the scenario set. Solving it gives the same
        # sizes, and every variable, exported or not.
        growth = orch.result("growth")
        model = orch.build(growth.spec_tag, **growth.overrides)

        print(f"Rebuilt growth: {solve(model).status}")
        july = model.horizon[2030, 7]
        model.display()
        print(f"growth battery: {pyo.value(july.battery.energy_capacity):.1f} kWh")
        flow = [pyo.value(v) for v in july.pump.inlet_state.flow_vol_phase[:, "Liq"]]
        print(f"growth July 2030 demand peaks at {max(flow):.1f} m^3/hr")
