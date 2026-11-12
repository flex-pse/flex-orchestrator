"""Size equipment across a multi-year study, from a study config.

The pump's demand comes from a projection per modeled year, made from a year
of meter data (10% more every five years) and stored as a dataset each. Each
horizon (a modeled year and month) is set from its own month of its year's
projection. The results go to a folder on disk, with no Acquirium.

    python examples/design_study/example.py
"""

import json
import os
import shutil
from pathlib import Path

import numpy as np
import pandas as pd

from flexorchestrator import Orchestrator
from flexorchestrator.backend import FileBackend

HERE = Path(__file__).parent
FOLDER = HERE / "on_disk"


def metered_demand() -> pd.DataFrame:
    """The pump's flow in January and July 2025, every 15 minutes, in m^3/hr.

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


def projection(metered: pd.DataFrame, year: int) -> pd.DataFrame:
    """The 2025 data moved to ``year`` and grown 10% every five years."""
    projected = metered * 1.10 ** ((year - 2025) / 5)
    projected.index = metered.index + pd.DateOffset(years=year - 2025)
    return projected


if __name__ == "__main__":
    os.chdir(HERE)  # the config names sun_july.json relative to here
    shutil.rmtree(FOLDER, ignore_errors=True)  # start afresh each time
    with Orchestrator(FileBackend(FOLDER)) as orch:
        orch.create_study("sizing")
        orch.store_model_spec("plant", json.loads(Path("flowsheet.json").read_text()))
        metered = metered_demand()
        orch.store_data("metered_demand", metered, units={"flow": "m^3/hr"})
        for year in (2030, 2035):  # one dataset per modeled year
            projected = projection(metered, year)
            orch.store_data(f"projection_{year}", projected, units={"flow": "m^3/hr"})
        run = orch.execute_study("study.yaml")
        print(run.counts())
        for task in run.tasks:
            summary = orch.design_results(task.tag)
            sizes = {
                name: f"{capacity.size:.1f} {capacity.units}"
                for name, capacity in summary.capacities.items()
            }
            print(f"{task.tag}: {sizes}, {summary.annualized_cost:,.0f} per year")

        # Each horizon read its own month of its year's projection.
        result = orch.result("solar_600")
        for source in result.model_inputs:
            window, dataset = source["window"], source["resolved"]["dataset"]
            [load] = (result.overrides[path]["value"] for path in source["paths"])
            print(
                f"{source['scope']} read {dataset} {window['start']} -> "
                f"{window['end']}: mean demand {np.mean(load):.1f} m^3/hr"
            )

        july = orch.model_results("solar_600")["horizon[2030,7].battery.charge"]
        print(f"July 2030 battery charge starts {july.timestamps[0]}")
