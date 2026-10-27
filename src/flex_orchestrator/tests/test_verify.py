"""``verify``: a store's counts, tags and value hashes, and its problems (M6).

On memory and files. The restore drill (``deploy/tenant``) compares a report
taken before a deletion with one taken after the restore.
"""

import json

import numpy as np
import pandas as pd
import pytest

from flex_orchestrator.cli import main
from flex_orchestrator.export import ExportedSeries, ModelData
from flex_orchestrator.hooks import SolveOutcome
from flex_orchestrator.orchestrator import Orchestrator
from flex_orchestrator.verify import REPORT, compare, verify

OK = SolveOutcome(status="ok", termination="optimal", solver="fake")


def cost(value):
    return ModelData(
        [
            ExportedSeries(
                key="costing.cost",
                kind="scalar",
                component="costing.cost",
                index=[],
                values=[value],
                timestamps=None,
                unit_pyomo="USD",
                unit_qudt=None,
            )
        ]
    )


@pytest.fixture
def filled(orch):
    hours = pd.date_range("2025-07-08", periods=4, freq="h", tz="UTC")
    orch.store_data("ops", pd.DataFrame({"q": np.arange(4.0)}, hours))
    orch.store_model_spec("base", {"value": 1})
    orch.store_model_data("a", cost(1.0), OK, spec="base")
    orch.store_model_data("b", cost(2.0), OK)
    return orch


def test_a_store_is_counted_and_fingerprinted(filled):
    report = verify(filled)
    assert report["report"] == REPORT and report["problems"] == []
    study = report["studies"][filled.study]
    assert study["result"]["runs"] == {"succeeded": 2}
    assert study["result"]["tags"] == ["a", "b"]
    assert study["data"]["tags"] == ["ops"] and study["model_spec"]["tags"] == ["base"]
    hashes = study["result"]["hashes"]
    assert set(hashes) == {"a", "b"} and hashes["a"]["hash"] != hashes["b"]["hash"]
    assert hashes["a"]["run_id"] == filled.result("a").run_id
    # The same store gives the same report, apart from when it was taken.
    assert compare(report, verify(filled)) == []


def test_samples_limit_what_is_read(filled):
    study = verify(filled, samples=1)["studies"][filled.study]
    assert list(study["result"]["hashes"]) == ["a"]
    assert study["result"]["tags"] == ["a", "b"]  # still every tag


def test_differences_after_a_change(filled):
    before = verify(filled)
    filled.delete("result", "b")
    filled.store_model_data("c", cost(3.0), OK)
    filled.store_model_data("a", cost(9.0), OK, replace=True)
    differences = compare(before, verify(filled))
    where = f"{filled.study} result"
    assert f"{where}: tags missing ['b']" in differences
    assert f"{where}: tags added ['c']" in differences
    assert f"{where} 'a': its values differ" in differences
    name = filled.study
    filled.create_study(name + "-new")
    after = verify(filled)
    assert f"study {name + '-new'!r} wasn't there before" in compare(before, after)
    assert f"study {name!r} is missing" in compare(
        after, verify(filled, studies=[name + "-new"])
    )


def test_problems(filled):
    runs = filled._runs
    stray = filled.backend.start_run(
        runs, {"flexorch_kind": "result", "flexorch_tag": "a"}
    )
    filled.backend.finish_run(stray)  # a second succeeded "a", which D9 forbids
    filled.backend.start_run(runs, {"flexorch_kind": "result", "flexorch_tag": "z"})
    problems = verify(filled)["problems"]
    assert f"{filled.study} result 'a' is stored 2 times" in problems
    assert f"{filled.study} result: runs still running ['z']" in problems


def test_an_unreadable_item_is_a_problem(filled, monkeypatch):
    real = filled.backend.records

    def broken(study, run_id, labels=None):
        if study == filled._runs:
            raise OSError("artifact missing")
        return real(study, run_id, labels)

    monkeypatch.setattr(filled.backend, "records", broken)
    problems = verify(filled)["problems"]
    assert f"{filled.study} result 'a' can't be read: artifact missing" in problems


@pytest.mark.unit
def test_the_cli_compares_and_exits_1_on_a_difference(tmp_path, capsys):
    from flex_orchestrator.backend import FileBackend

    store = tmp_path / "store"
    with Orchestrator(FileBackend(store), environ={}) as orch:
        orch.create_study("s")
        orch.store_model_data("a", cost(1.0), OK)
    assert main(["--files", str(store), "verify"]) == 0
    before = tmp_path / "before.json"
    before.write_text(capsys.readouterr().out)
    assert json.loads(before.read_text())["ok"] is True
    assert main(["--files", str(store), "verify", "--against", str(before)]) == 0
    capsys.readouterr()
    with Orchestrator(FileBackend(store), study="s", environ={}) as orch:
        orch.delete("result", "a")
    assert main(["--files", str(store), "verify", "--against", str(before)]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is False
    assert report["differences"] == [
        "s result: runs {'succeeded': 1} -> {}",
        "s result: tags missing ['a']",
    ]
