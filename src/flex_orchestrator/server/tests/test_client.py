"""The client for a deployed tenant, ``Orchestrator(url=...)`` (PLAN.md D15, M7).

Against the real app, in-process: the client's requests go through FastAPI's
TestClient instead of the network.
"""

import numpy as np
import pandas as pd
import polars as pl
import pytest
from fastapi.testclient import TestClient

from flex_orchestrator import Orchestrator
from flex_orchestrator.client import RemoteOrchestrator, read_token, save_token
from flex_orchestrator.errors import (
    AccessError,
    CommandError,
    StudyExistsError,
    StudyNotFoundError,
    TagExistsError,
    TagNotFoundError,
)
from flex_orchestrator.server.testing import make_test_app, token_for
from flex_orchestrator.server.tests.test_server import HOOKS, PLUGIN

pytestmark = pytest.mark.unit

URL = "http://tenant.test"


@pytest.fixture
def client():
    with TestClient(make_test_app(plugins=[PLUGIN])) as client:
        yield client


def remote(client, role="owner", **kwargs):
    def send(method, url, headers, body):
        found = client.request(
            method, url.removeprefix(URL), headers=headers, content=body
        )
        return found.status_code, found.content

    return RemoteOrchestrator(URL, token_for(role), transport=send, **kwargs)


def frame():
    hours = pd.date_range("2025-07-08", periods=24, freq="h", tz="UTC")
    q = np.linspace(10, 50, 24)
    return pd.DataFrame({"q": q, "p": 3 * q + 1}, index=hours)


def test_a_study_over_http(client, tmp_path):
    orch = remote(client)
    assert orch.create_study("s") == "s" and orch.studies() == ["s"]
    orch.store_data("ops", frame(), units={"q": "m^3/hr", "p": "kW"})
    back = orch.get_data("ops", frame="pandas")
    pd.testing.assert_frame_equal(back, frame().rename_axis("time"), check_freq=False)
    assert isinstance(orch.get_data("ops", columns=["q"]), pl.DataFrame)
    spec = orch.store_model_spec("base", {"value": 1.0})
    assert orch.model_spec("base").spec_hash == spec.spec_hash
    assert [s.tag for s in orch.stored_specs()] == ["base"]

    run = orch.execute_study(
        {
            "base_model_spec": "base",
            "hooks": HOOKS,
            "sweeps": [
                {"type": "variable", "name": "v", "path": "p.v", "values": [1, 2]}
            ],
            "export": {"exclude": ["pid", "threads"]},
        }
    )
    assert run.counts() == {"succeeded": 2}
    results = orch.stored_model_results()
    assert {r.tag: r.scalars() for r in results} == {
        "base_1": {"x": 2.0},
        "base_2": {"x": 3.0},
    }
    assert orch.result("base_2").metadata["flexorch_user"] == "tester"
    fit = orch.fit_surrogate(
        tag="lin",
        regressor="multilinear",
        X={"dataset": "ops", "columns": ["q"]},
        y={"dataset": "ops", "columns": ["p"]},
    )
    assert [f["tag"] for f in fit["fits"]] == ["lin"]
    assert orch.surrogate_spec("lin").kind == "surrogate_spec"

    folder = orch.export_study(tmp_path / "export")
    assert (folder / "study.json").is_file()
    report = orch.import_study(folder, name="copy")
    assert report["study"] == "copy" and report["counts"]["results"] == 2
    orch.select_study("copy")
    assert [r.tag for r in orch.stored_model_results()] == ["base_1", "base_2"]
    orch.delete("result", "base_1")
    assert [r.tag for r in orch.stored_model_results()] == ["base_2"]
    orch.delete_study("copy")
    assert orch.studies() == ["s"]


def test_commands_and_jobs(client):
    orch = remote(client)
    names = {c["name"] for c in orch.commands()}
    assert {"echo", "slow", "execute_study"} <= names
    assert orch.run_command("echo", text="hi") == {"text": "hi", "user": "tester"}
    assert orch.run_command("slow", {"text": "later"}) == "later"
    queued = orch.run_command("slow", text="x", wait=False)
    orch.cancel(queued["job_id"])
    with pytest.raises(CommandError, match="cancelled"):
        orch.wait(queued["job_id"])


def test_errors_are_the_librarys(client):
    orch = remote(client)
    with pytest.raises(StudyNotFoundError):
        orch.select_study("nope")
    with pytest.raises(StudyNotFoundError, match="No study is selected"):
        orch.model_spec("x")
    orch.create_study("s")
    with pytest.raises(StudyExistsError):
        orch.create_study("s")
    orch.store_model_spec("base", {"value": 1})
    with pytest.raises(TagExistsError):
        orch.store_model_spec("base", {"value": 2})
    with pytest.raises(TagNotFoundError):
        orch.result("nope")
    with pytest.raises(CommandError):
        orch.run_command("echo", wrong=1)
    viewer = remote(client, role="viewer", study="s")
    with pytest.raises(AccessError) as refused:
        viewer.delete("model_spec", "base")
    assert refused.value.status == 403


def test_tokens(client, tmp_path, monkeypatch):
    def send(method, url, headers, body):
        found = client.request(method, url.removeprefix(URL), headers=headers)
        return found.status_code, found.content

    stranger = RemoteOrchestrator(URL, "wrong", transport=send)
    with pytest.raises(AccessError) as refused:
        stranger.studies()
    assert refused.value.status == 401
    assert "wrong" not in repr(stranger)
    monkeypatch.setattr("flex_orchestrator.client.TOKEN_FILE", tmp_path / "token")
    with pytest.raises(AccessError, match="No token"):
        RemoteOrchestrator(URL, environ={}, transport=send).studies()
    assert read_token({"FLEXORCH_TOKEN": "from-env"}) == "from-env"
    path = save_token("saved-token\n", tmp_path / "token")
    assert read_token({}) == "saved-token"
    assert RemoteOrchestrator(URL, environ={})._token == "saved-token"
    assert path.read_text() == "saved-token\n"


def test_orchestrator_with_a_url_is_the_client():
    orch = Orchestrator(url=URL, token="t", study=None)
    assert isinstance(orch, RemoteOrchestrator) and orch.url == URL
    with pytest.raises(FileNotFoundError):  # a gateway's CA, checked up front
        Orchestrator(url=URL, token="t", cafile="no-such-ca.crt")
    with pytest.raises(ValueError, match="takes only"):
        Orchestrator(url=URL, token="t", data_dir="x")
    with pytest.raises(ValueError, match="goes with url"):
        Orchestrator(token="t")
