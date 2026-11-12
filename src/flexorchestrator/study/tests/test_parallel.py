"""Studies and fits on Ray, on a local Ray with 4 CPUs.

The hooks are in ``ray_hooks.py``, so workers import them by name. Results
go to a memory backend: the main process stores everything, so the workers
never need it.
"""

import time

import pandas as pd
import pytest

from flexorchestrator.errors import CommandCancelled
from flexorchestrator.ext import Progress
from flexorchestrator.study.tests.ray_hooks import HOOKS
from flexorchestrator.testing import memory_orchestrator

ray = pytest.importorskip("ray")
pytestmark = [pytest.mark.component, pytest.mark.timeout(600)]


@pytest.fixture(scope="module", autouse=True)
def local_ray():
    started = not ray.is_initialized()
    if started:
        ray.init(
            address="local",
            namespace="flexorch",
            include_dashboard=False,
            num_cpus=4,
            log_to_driver=False,
        )
    yield
    if started:
        ray.shutdown()


@pytest.fixture
def orch():
    orch = memory_orchestrator(study="s")
    orch.store_model_spec("base", {"value": 1.0})
    orch.store_model_spec("other", {"value": 10.0})
    return orch


def study(values=(1, 2, 3, 4, 5, 6), **fields):
    return {
        "base_model_spec": "base",
        "hooks": HOOKS,
        "sweeps": [
            {"type": "spec_list", "specs": ["base", "other"]},
            {"type": "variable", "name": "v", "path": "p.v", "values": list(values)},
        ],
        "output_name": "{spec}_{v}",
        "export": {"exclude": ["process", "threads"]},
        **fields,
    }


def stored(orch):
    """``{tag: (scalars, units, spec tag, overrides, solver)}`` of every result."""
    return {
        r.tag: (r.scalars(), r.units, r.spec_tag, r.overrides, r.outcome.solver)
        for r in orch.stored_model_results()
    }


def test_the_same_results_with_one_worker_and_four():
    serial, parallel = memory_orchestrator(study="s"), memory_orchestrator(study="s")
    for orch in (serial, parallel):
        orch.store_model_spec("base", {"value": 1.0})
        orch.store_model_spec("other", {"value": 10.0})
    one = serial.execute_study(study())
    four = parallel.execute_study(study(), max_workers=4)
    assert one.counts() == four.counts() == {"succeeded": 12}
    assert [r.tag for r in four.results] == [r.tag for r in one.results]
    assert stored(parallel) == stored(serial)
    assert stored(parallel)["other_6"][0] == {"x": 16.0}
    meta = parallel.result("other_6").metadata["flexorch_hooks"]
    assert meta["build"] == HOOKS["build"]


def test_model_inputs_reach_workers_as_values():
    # Resolved in the main process; a worker gets only the number.
    runs = {}
    for workers in (1, 2):
        orch = memory_orchestrator(study="s")
        orch.store_model_spec("base", {"value": 1.0})
        hours = pd.date_range("2025-07-08", periods=3, freq="h", tz="UTC")
        orch.store_data("ops", pd.DataFrame({"x": [2.0, 3.0, 4.0]}, hours))
        config = study(values=(1, 2))
        config["sweeps"] = config["sweeps"][1:]
        config["output_name"] = "{v}"
        config["model_inputs"] = [{"dataset": "ops", "reduce": "max"}]
        assert orch.execute_study(config, max_workers=workers).counts() == {
            "succeeded": 2
        }
        runs[workers] = stored(orch)
    assert runs[2] == runs[1]
    assert runs[2]["2"][0] == {"x": 7.0}  # 1 + 2 + the stored max, 4
    assert runs[2]["2"][3] == {"p.v": 2, "x": 4.0}


def test_workers_run_one_thread_per_library(orch):
    run = orch.execute_study(study(values=[1], export={}), max_workers=2)
    assert run.counts() == {"succeeded": 2}
    assert {orch.result(t).scalars()["threads"] for t in ("base_1", "other_1")} == {1.0}


def test_max_calls_restarts_workers(orch):
    cfg = study(export={}, execution={"max_calls": 1})
    orch.execute_study(cfg, max_workers=2)
    # One process per task: each was a new worker, so each drew its own token.
    processes = [r.scalars()["process"] for r in orch.stored_model_results()]
    assert len(processes) == 12 and len(set(processes)) == 12


def test_a_hung_task_is_killed_at_its_timeout_and_the_sweep_goes_on(orch):
    cfg = {
        "base_model_spec": "base",
        "hooks": HOOKS,
        "sweeps": [{"type": "variable", "path": "sleep", "values": [0, 600, 0, 0]}],
        "output_name": "t{index}",
        "execution": {"task_timeout_s": 20},
    }
    started = time.monotonic()
    run = orch.execute_study(cfg, max_workers=2)
    assert time.monotonic() - started < 60
    assert [r.status for r in run.results] == ["succeeded", "error", "succeeded"] + [
        "succeeded"
    ]
    hung = run.result("t1")
    assert hung.outcome.termination == "timeout"
    assert "killed after 20 s" in hung.error
    assert [r.tag for r in orch.stored_model_results()] == ["t0", "t2", "t3"]


def test_a_dead_worker_is_retried(orch, tmp_path):
    marker = str(tmp_path / "crashed")
    cfg = {
        "base_model_spec": "base",
        "hooks": HOOKS,
        "sweeps": [{"type": "variable", "path": "crash_once", "values": [marker]}],
        "output_name": "crash",
    }
    run = orch.execute_study({**cfg, "execution": {"retries": 1}}, max_workers=2)
    [result] = run.results
    assert result.status == "succeeded" and result.attempts == 2
    no_retry = orch.execute_study(
        {
            **cfg,
            "output_name": "crash2",
            "sweeps": [
                {"type": "variable", "path": "crash_once", "values": [marker + "2"]}
            ],
        },
        max_workers=2,
    )
    [result] = no_retry.results
    assert result.status == "error" and result.attempts == 1
    assert "Worker" in result.error or "died" in result.error.lower()


def test_errors_and_on_error(orch):
    cfg = {
        "base_model_spec": "base",
        "hooks": HOOKS,
        "sweeps": [{"type": "variable", "path": "fail", "values": [0, 1, 0]}],
        "output_name": "f{index}",
    }
    run = orch.execute_study(cfg, max_workers=2)
    assert [r.status for r in run.results] == ["succeeded", "error", "succeeded"]
    assert "build failed on purpose" in run.result("f1").error
    cfg["sweeps"].append({"type": "variable", "path": "sleep", "values": [5]})
    cfg["output_name"] = "g{index}"
    stop = orch.execute_study({**cfg, "execution": {"on_error": "stop"}}, max_workers=3)
    assert "error" in [r.status for r in stop.results]
    assert len(stop.results) < 3  # the others were cancelled


def test_cancelling_cancels_running_tasks(orch):
    progress = Progress(
        lambda report: progress.cancel() if report["done"] >= 1 else None
    )
    cfg = {
        "base_model_spec": "base",
        "hooks": HOOKS,
        "sweeps": [{"type": "variable", "path": "sleep", "values": [0, 600, 600]}],
        "output_name": "c{index}",
    }
    started = time.monotonic()
    with pytest.raises(CommandCancelled):
        orch.execute_study(cfg, max_workers=3, progress=progress)
    assert time.monotonic() - started < 60
    assert [r.tag for r in orch.stored_model_results()] == ["c0"]


def test_many_tasks_store_each_tag_once(orch):
    run = orch.execute_study(study(values=range(25)), max_workers=4)
    assert run.counts() == {"succeeded": 50}
    tags = [r.tag for r in orch.stored_model_results()]
    assert len(tags) == len(set(tags)) == 50


def test_fit_variants_on_ray_match_fitting_here():
    pytest.importorskip("flexparameterize")
    times = pd.date_range("2025-01-01", periods=48, freq="h", tz="UTC")
    frame = pd.DataFrame({"q": range(1, 49)}, times, dtype=float)
    frame["p"] = 2.0 * frame["q"] + 1
    fit = {
        "tag": "fit_{name}",
        "X": {"dataset": "d", "columns": ["q"]},
        "y": {"dataset": "d", "columns": ["p"]},
        "validation": {"holdout": "6h"},
        "store_series": True,
        "variants": [
            {"name": "linear", "regressor": "multilinear"},
            {"name": "average", "regressor": "constant_intensity"},
            {
                "name": "late",
                "regressor": "multilinear",
                "window": {"start": times[24]},
            },
        ],
    }
    summaries = []
    for workers in (1, 3):
        orch = memory_orchestrator(study="s")
        orch.store_data("d", frame, units={"q": "m^3/hr", "p": "kW"})
        summary = orch.fit_surrogate(**fit, max_workers=workers)
        specs = {s.tag: s.spec_hash for s in orch.stored_specs("surrogate_spec")}
        summaries.append(
            ([r.model_dump(exclude={"run_id"}) for r in summary.fits], specs)
        )
    assert summaries[0] == summaries[1]
    assert summaries[0][0][0]["coefficients"]["q"] == pytest.approx(2.0)


def test_skips_prepare_errors_and_resources(orch):
    from flexorchestrator.study.tests.ray_hooks import HOOKS as hooks

    orch.execute_study(study(values=[1]), max_workers=2)
    cfg = study(
        values=[1, 2],
        on_existing="skip",
        execution={"resources": {"num_cpus": 1, "memory": 2**28}},
    )
    run = orch.execute_study(cfg, max_workers=2)
    assert run.counts() == {"skipped": 2, "succeeded": 2}
    missing = {
        "base_model_spec": "base",
        "hooks": hooks,
        "sweeps": [{"type": "surrogate", "unit": "u", "surrogates": ["nope"]}],
    }
    [result] = orch.execute_study(missing, max_workers=2).results
    assert result.status == "error" and "nope" in result.error


def test_a_failing_job_cancels_the_others():
    from flexorchestrator.parallel import map_ordered
    from flexorchestrator.study.tests.ray_hooks import boom

    assert map_ordered(boom, [(0,), (0,)], workers=2) == [0, 0]
    started = time.monotonic()
    with pytest.raises(Exception, match="job 1 failed"):
        map_ordered(boom, [(600,), (1,)], workers=2)
    assert time.monotonic() - started < 60


@pytest.mark.needs_highs
def test_design_study_runs_on_ray():
    """With each horizon's load from stored data, Ray stores what serial does."""
    pytest.importorskip("flexdesign")
    from flexorchestrator.hooks import DEFAULT_HOOKS
    from flexorchestrator.study.tests.test_study_design import (
        design,
        flowsheet,
        load_input,
        meter,
    )

    runs = []
    for workers in (1, 2):
        orch = memory_orchestrator(hooks=DEFAULT_HOOKS)
        orch.store_model_spec("plant", flowsheet())
        orch.store_data("meter", meter(), units={"flow": "m^3/hr"})
        run = orch.execute_study(
            {
                "base_model_spec": "plant",
                "design": design(),
                "model_inputs": [load_input()],
                "sweeps": [
                    {
                        "type": "configuration_option",
                        "name": "cost",
                        "path": "capacities.solar.capital_cost",
                        "values": [600.0, 2400.0],
                    }
                ],
                "output_name": "ray_{cost:.0f}",
                "execution": {"max_workers": workers},
            }
        )
        assert run.counts() == {"succeeded": 2}, run.results
        runs.append(
            {
                tag: (
                    orch.result(tag).overrides,
                    orch.design_results(tag).annualized_cost,
                )
                for tag in ("ray_600", "ray_2400")
            }
        )
    serial, on_ray = runs
    assert {t: o for t, (o, _) in on_ray.items()} == {
        t: o for t, (o, _) in serial.items()
    }
    for tag, (_, cost) in on_ray.items():
        assert cost == pytest.approx(serial[tag][1]) and cost > 0
