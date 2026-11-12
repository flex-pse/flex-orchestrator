"""Model inputs in studies and builds.

On memory and files: a ``model_inputs`` section, a ``model_inputs`` sweep and
a case, what each result records, the clash with a ``variable`` sweep, export
and import, and ``orch.apply_model_inputs``. A tiny Pyomo model stands in for
flex-pse; its ``time_grid`` hook reads the spec's ``start`` and ``steps``.
"""

import numpy as np
import pandas as pd
import pyomo.environ as pyo
import pytest

from flexorchestrator.archive import load_result
from flexorchestrator.errors import ModelInputError, StudyConfigError
from flexorchestrator.hooks import HookSet
from flexorchestrator.orchestrator import Orchestrator
from flexorchestrator.study import StudyConfig, expand
from flexorchestrator.testing import fake_solve


def tiny_build(spec, **overrides):
    m = pyo.ConcreteModel()
    m.inputs = pyo.Var(initialize=len(overrides))
    m.spec, m.applied = spec, {}
    return m


def hourly_grid(spec_or_model):
    """The spec's hours; with ``scopes``, a time axis per scope, like a design."""
    spec = getattr(spec_or_model, "spec", spec_or_model)
    if "scopes" in spec:
        return {
            scope: pd.date_range(start, periods=spec["steps"], freq="h")
            for scope, start in spec["scopes"].items()
        }
    return pd.date_range(spec["start"], periods=spec["steps"], freq="h")


def record_values(model, values):
    model.applied.update(values)


HOOKS = HookSet(
    build=tiny_build,
    solve=fake_solve,
    time_grid=hourly_grid,
    apply_values=record_values,
)
DEMAND = {"dataset": "ops", "columns": ["demand"], "rename": {"demand": "plant.d"}}
LINES = {
    "type": "model_inputs",
    "name": "line",
    "values": [
        {
            "label": "a",
            "inputs": [
                {
                    "dataset": "ops",
                    "columns": ["feed_a"],
                    "rename": {"feed_a": "plant.f"},
                }
            ],
        },
        {
            "label": "b",
            "inputs": [
                {
                    "dataset": "ops",
                    "columns": ["feed_b"],
                    "rename": {"feed_b": "plant.f"},
                },
                {**DEMAND, "reduce": "first"},  # replaces the study's demand
            ],
        },
    ],
}


@pytest.fixture
def orch(orch_backend, study_name):
    orch = Orchestrator(orch_backend, hooks=HOOKS, environ={})
    orch.create_study(study_name)
    hours = pd.date_range("2025-07-07", periods=72, freq="h", tz="UTC")
    orch.store_data(
        "ops",
        pd.DataFrame(
            {
                "demand": np.arange(72.0),
                "feed_a": np.full(72, 1.0),
                "feed_b": np.full(72, 2.0),
            },
            hours,
        ),
        units={"demand": "m^3/hr"},
    )
    orch.store_model_spec("base", {"start": "2025-07-08", "steps": 3})
    return orch


def study(**fields):
    return {
        "base_model_spec": "base",
        "model_inputs": [DEMAND],
        "sweeps": [LINES],
        "output_name": "line_{line}",
        **fields,
    }


def test_a_study_sets_variables_from_stored_data(orch):
    run = orch.execute_study(study())
    assert run.counts() == {"succeeded": 2}
    a, b = orch.result("line_a"), orch.result("line_b")
    assert a.overrides == {
        "plant.d": {"value": [24.0, 25.0, 26.0], "units": "m^3/hr"},
        "plant.f": [1.0, 1.0, 1.0],
    }
    assert b.overrides["plant.d"] == {"value": 24.0, "units": "m^3/hr"}
    assert b.overrides["plant.f"] == [2.0, 2.0, 2.0]
    assert [m["paths"] for m in a.model_inputs] == [["plant.d"], ["plant.f"]]
    # b's own demand entry replaced the study's, so only it is recorded.
    assert [m["paths"] for m in b.model_inputs] == [["plant.f"], ["plant.d"]]
    assert b.model_inputs[1]["input"]["reduce"] == "first"
    data_run = a.model_inputs[0]["resolved"]["run_id"]
    assert data_run == orch._latest("ops", "data").run_id


def test_cases_set_model_inputs_too(orch):
    case = {
        "name": "feed_b",
        "model_inputs": [
            {"dataset": "ops", "columns": ["feed_b"], "rename": {"feed_b": "plant.f"}}
        ],
    }
    run = orch.execute_study(
        study(sweeps=[{"type": "cases", "cases": [case]}], output_name="{case}")
    )
    assert run.counts() == {"succeeded": 1}
    assert orch.result("feed_b").overrides["plant.f"] == [2.0, 2.0, 2.0]


def test_a_variable_set_both_ways_is_an_error(orch):
    sweep = {"type": "variable", "path": "plant.d", "values": [1.0]}
    run = orch.execute_study(study(sweeps=[sweep], output_name="v"))
    [result] = run.results
    assert result.status == "error"
    assert "set both by model_inputs and by a variable" in result.error


def test_each_time_block_gets_its_own_data(orch):
    scoped = {"scopes": {"h1": "2025-07-08", "h2": "2025-07-09"}, "steps": 2}
    orch.store_model_spec("scoped", scoped)
    run = orch.execute_study(
        study(base_model_spec="scoped", sweeps=[], output_name="scoped")
    )
    assert run.counts() == {"succeeded": 1}
    result = orch.result("scoped")
    assert {path: v["value"] for path, v in result.overrides.items()} == {
        "h1.plant.d": [24.0, 25.0],
        "h2.plant.d": [48.0, 49.0],
    }
    assert [(m["scope"], m["paths"]) for m in result.model_inputs] == [
        ("h1", ["h1.plant.d"]),
        ("h2", ["h2.plant.d"]),
    ]
    # A variable sweep of the same path collides with every time block's.
    sweep = {"type": "variable", "path": "plant.d", "values": [1.0]}
    run = orch.execute_study(
        study(base_model_spec="scoped", sweeps=[sweep], output_name="clash")
    )
    [clash] = run.results
    assert clash.status == "error" and "['plant.d'] are set both" in clash.error


def test_a_failed_input_is_an_error_result(orch):
    run = orch.execute_study(
        study(model_inputs=[{**DEMAND, "offset": "3650D"}], sweeps=[], output_name="x")
    )
    [result] = run.results
    assert result.status == "error" and "ModelInputError" in result.error


def test_the_plan_carries_the_entries(orch):
    plan = orch.execute_study(study(), dry_run=True)
    [a, b] = plan.tasks
    assert a.labels == {"line": "a"} and a.model_inputs[0].dataset == "ops"
    assert [i.columns for i in b.swept_inputs] == [["feed_b"], ["demand"]]
    assert orch.stored_model_results() == []


def test_export_and_import_keep_where_inputs_came_from(orch, tmp_path):
    orch.execute_study(study())
    folder = orch.export_study(tmp_path / "e").path
    loaded = load_result(folder / "results" / "line_a.json")
    assert loaded.model_inputs == orch.result("line_a").model_inputs
    assert loaded.overrides == orch.result("line_a").overrides
    orch.import_study(folder, name=orch.study + "-copy")
    assert orch.result("line_a").model_inputs == loaded.model_inputs


def test_inputs_applied_to_a_built_model(orch):
    model = orch.build("base", **{"plant.x": 1.0})
    found = orch.apply_model_inputs(model, DEMAND)  # one entry, or a list
    assert model.applied == found.values
    assert model.applied["plant.d"]["value"] == [24.0, 25.0, 26.0]
    feed = {"dataset": "ops", "columns": ["feed_a"], "rename": {"feed_a": "plant.f"}}
    orch.apply_model_inputs(model, [feed, {**DEMAND, "reduce": "last"}])
    assert model.applied["plant.d"] == {"value": 26.0, "units": "m^3/hr"}
    stored = orch.store_model_results("built", model, fake_solve(model))
    result = orch.result("built")
    assert stored.ok and result.overrides["plant.x"] == 1.0
    # The second apply replaced plant.d, so only it is recorded for that path.
    assert result.overrides["plant.d"]["value"] == 26.0
    assert [m["paths"] for m in result.model_inputs] == [["plant.f"], ["plant.d"]]
    with pytest.raises(ModelInputError, match="set both"):
        orch.apply_model_inputs(orch.build("base", **{"plant.d": 1.0}), DEMAND)
    found = orch.resolve_model_inputs("base", [{**DEMAND, "reduce": "mean"}])
    assert found.values == {"plant.d": {"value": 25.0, "units": "m^3/hr"}}


@pytest.mark.unit
def test_sweep_labels_name_tasks_and_must_differ():
    tasks = expand(StudyConfig.model_validate({**study(), "output_name": None}))
    assert [t.tag for t in tasks] == ["base_a", "base_b"]
    repeated = {**LINES, "values": [LINES["values"][0]] * 2}
    with pytest.raises(StudyConfigError, match="labels must differ"):
        from flexorchestrator.study import load_config

        load_config({"base_model_spec": "base", "sweeps": [repeated]})
    schema = StudyConfig.model_json_schema()
    assert "model_inputs" in schema["properties"]
    assert "ModelInputsSweep" in schema["$defs"]
