"""Model inputs on flex-pse models, solved.

The demo plant's pump flow comes from a stored dataset (memory, files and
Acquirium) and, on Acquirium, from historian streams; every result records
where its inputs came from.
"""

import uuid
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pyomo.environ as pyo
import pytest

from flexorchestrator.backend import AcquiriumBackend
from flexorchestrator.orchestrator import Orchestrator

pytest.importorskip("flexops")
pytestmark = [pytest.mark.component, pytest.mark.needs_highs]

FLOW = "demo.pump.flow_in"
# The demo's horizon is 2025-07-08 00:00 to 06:00, hourly.
HOURS = pd.date_range("2025-07-07", periods=48, freq="h", tz="UTC")


def test_time_grid_matches_the_built_model(demo_spec):
    from flexorchestrator.flexpse import build, time_grid

    grid = time_grid(demo_spec)
    model = build(demo_spec)
    assert list(grid) == list(model.demo.time_block._datetime_index)
    assert time_grid({"elements": []}) is None


@pytest.mark.on_acquirium
def test_a_study_takes_the_pump_flow_from_a_dataset(orch, demo_spec):
    flow = 3.0 + (np.arange(48) % 24) / 4  # 3 at midnight, rising 0.25 an hour
    flow[:24] += 1.0  # the day before runs 1 m^3/hr higher
    orch.store_data(
        "pump", pd.DataFrame({"flow": flow}, HOURS), units={"flow": "m^3/hr"}
    )
    orch.store_model_spec("demo", demo_spec)
    pump = {"dataset": "pump", "columns": ["flow"], "rename": {"flow": FLOW}}
    run = orch.execute_study(
        {
            "base_model_spec": "demo",
            "model_inputs": [pump],
            "sweeps": [
                {
                    "type": "model_inputs",
                    "name": "day",
                    "values": [
                        {"label": "today", "inputs": [pump]},
                        {"label": "yesterday", "inputs": [{**pump, "offset": "-1D"}]},
                    ],
                }
            ],
            "export": {"include": [FLOW, "costing.aggregate_operating_cost"]},
            "output_name": "{day}",
        }
    )
    assert run.counts() == {"succeeded": 2}
    today = orch.result("today").to_pandas()[FLOW].tolist()
    yesterday = orch.result("yesterday").to_pandas()[FLOW].tolist()
    assert today == [3.0, 3.25, 3.5, 3.75, 4.0, 4.25]
    assert yesterday == [v + 1.0 for v in today]
    [record] = orch.result("yesterday").model_inputs
    assert record["window"]["start"] == "2025-07-07 00:00:00"
    assert record["resolved"]["run_id"] == orch._latest("pump", "data").run_id


@pytest.fixture
def historian(acquirium_local):
    """Two pump-flow streams of one datasource, hourly over the demo's day."""
    key = uuid.uuid4().hex[:8]
    source = f"plant_{key}"
    acquirium_local.register_datasource(source)
    acquirium_local.register_streams(
        [
            {
                "point_uri": f"urn:test:{key}:{name}",
                "source_id": source,
                "ref_name": name,
                "label": f"pump flow {name} {key}",
                "unit": "M3-PER-HR",
                "value_kind": "numeric",
            }
            for name in ("FT_A", "FT_B")
        ]
    )
    start = datetime(2025, 7, 7, 22, tzinfo=UTC)
    for name, level in (("FT_A", 4.0), ("FT_B", 6.0)):
        rows = [(start + timedelta(hours=h), level + h / 10) for h in range(12)]
        acquirium_local.insert_timeseries(source, name, rows)
    return source


def test_streams_set_the_pump_flow_on_acquirium(
    acquirium_local, historian, study_name, demo_spec
):
    orch = Orchestrator(AcquiriumBackend(acquirium_local), environ={})
    orch.create_study(study_name)
    orch.store_model_spec("demo", demo_spec)

    def line(name):
        return {
            "streams": [{"source_id": historian, "ref_name": name}],
            "rename": {"*": FLOW},
        }

    model = orch.build("demo")
    orch.apply_model_inputs(model, line("FT_A"))
    # Midnight UTC is the stream's third sample: 4.0 + 2/10.
    assert [pyo.value(model.demo.pump.flow_in[i]) for i in range(2)] == [4.2, 4.3]
    run = orch.execute_study(
        {
            "base_model_spec": "demo",
            "sweeps": [
                {
                    "type": "model_inputs",
                    "name": "line",
                    "values": [
                        {"label": "a", "inputs": [line("FT_A")]},
                        {"label": "b", "inputs": [line("FT_B")]},
                    ],
                }
            ],
            "export": {"include": [FLOW]},
            "output_name": "line_{line}",
        }
    )
    assert run.counts() == {"succeeded": 2}
    b = orch.result("line_b")
    assert b.to_pandas()[FLOW].tolist()[0] == pytest.approx(6.2)
    [record] = b.model_inputs
    assert record["resolved"]["refs"] == {
        FLOW: str(acquirium_local.reference_uri(historian, "FT_B"))
    }


def two_plants():
    """The issue's model: two plants, each with its own time block."""
    from flexops import TimeBlock
    from pyomo.environ import units as pu

    m = pyo.ConcreteModel()
    m.plant_a = pyo.Block()
    m.plant_b = pyo.Block()
    m.plant_a.time_block = TimeBlock(
        start_date="2025-07-08", end_date="2025-07-08T03:00", time_step=pu.hr
    )
    m.plant_b.time_block = TimeBlock(
        start_date="2025-07-09", end_date="2025-07-09T02:00", time_step=pu.hr
    )
    m.plant_a.var_a = pyo.Var(m.plant_a.time_block.time_index)
    m.plant_b.var_b = pyo.Var(m.plant_b.time_block.time_index)
    m.plant_b.size = pyo.Var()
    return m


def test_each_plant_takes_data_over_its_own_time_block():
    from flexops import TimeBlock

    from flexorchestrator.errors import ModelInputError, SpecError
    from flexorchestrator.flexpse import apply_values, time_grid
    from flexorchestrator.hooks import DEFAULT_HOOKS
    from flexorchestrator.testing import memory_orchestrator

    model = two_plants()
    grids = time_grid(model)
    assert {scope: list(grid.hour) for scope, grid in grids.items()} == {
        "plant_a": [0, 1, 2],
        "plant_b": [0, 1],
    }
    assert grids["plant_b"][0] == pd.Timestamp("2025-07-09")

    orch = memory_orchestrator(hooks=DEFAULT_HOOKS)
    orch.store_data("meter", pd.DataFrame({"flow": np.arange(48.0)}, HOURS))
    meter = {"dataset": "meter", "columns": ["flow"]}
    a, b = model.plant_a, model.plant_b
    # 8 July 00:00 is hour 24 of the data.
    found = orch.apply_model_inputs(
        model, {**meter, "rename": {"flow": "plant_a.var_a"}}
    )
    assert [a.var_a[t].value for t in a.time_block.time_index] == [24.0, 25.0, 26.0]
    assert all(a.var_a[t].fixed for t in a.time_block.time_index)
    assert found.provenance[0]["scope"] == "plant_a"
    # plant_b runs on 9 July, past the data's end: its own window has no data.
    with pytest.raises(ModelInputError, match="2025-07-09 00:00:00"):
        orch.apply_model_inputs(model, {**meter, "rename": {"flow": "plant_b.var_b"}})
    found = orch.apply_model_inputs(
        model, {**meter, "rename": {"flow": "plant_b.var_b"}, "offset": "-1D"}
    )
    assert [b.var_b[t].value for t in b.time_block.time_index] == [24.0, 25.0]
    assert found.provenance[0]["scope"] == "plant_b"
    assert a.var_a[model.plant_a.time_block.time_index.first()].value == 24.0

    with pytest.raises(SpecError, match="under none of them"):
        apply_values(model, {"size": 1.0})
    apply_values(model, {"plant_b.size": 2.0})
    assert b.size.fixed and b.size.value == 2.0

    model.plant_a.second = TimeBlock(
        start_date="2025-07-08", end_date="2025-07-08T01:00", time_step=pyo.units.hr
    )
    with pytest.raises(SpecError, match="holds two time blocks"):
        time_grid(model)
