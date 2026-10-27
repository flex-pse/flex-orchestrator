"""Local mode, and specs with files over HTTP.

Local mode (``flex-orch serve --local``) takes no tokens: every request is the
local user, as owner, so it must refuse anything not from this machine.
"""

import pytest
from fastapi.testclient import TestClient

from flex_orchestrator import cli
from flex_orchestrator.backend import MemoryBackend
from flex_orchestrator.client import RemoteOrchestrator
from flex_orchestrator.errors import AccessError, SpecError
from flex_orchestrator.server import Settings, create_app, hash_token
from flex_orchestrator.server.settings import parse_api_tokens
from flex_orchestrator.server.testing import make_test_app, token_for
from flex_orchestrator.server.tests.test_server import HOOKS
from flex_orchestrator.testing.orchestrators import FAKE_HOOKS

pytestmark = pytest.mark.unit

LOCAL = "http://127.0.0.1:8080"


def local_app(backend=None):
    return create_app(
        Settings.local(
            "ana",
            orchestrator={"backend": backend or MemoryBackend(), "hooks": FAKE_HOOKS},
            environ={},
        )
    )


@pytest.fixture(autouse=True)
def no_saved_token(tmp_path, monkeypatch):
    """No token from ``flex-orch login``: only what each test passes."""
    monkeypatch.setattr("flex_orchestrator.client.TOKEN_FILE", tmp_path / "token")


@pytest.fixture
def local():
    """A local-mode app, as this machine sees it."""
    with TestClient(local_app(), base_url=LOCAL, client=("127.0.0.1", 50000)) as c:
        yield c


def remote(client, url, token=None):
    def send(method, full, headers, body):
        found = client.request(
            method, full.removeprefix(url), headers=headers, content=body
        )
        return found.status_code, found.content

    return RemoteOrchestrator(url, token, transport=send, environ={})


def test_local_mode_is_the_local_user_as_owner(local):
    orch = remote(local, LOCAL)  # no token
    orch.create_study("s")
    orch.store_model_spec("base", {"value": 1.0})
    run = orch.execute_study(
        {"base_model_spec": "base", "hooks": HOOKS, "output_name": "solved"}
    )
    assert run.counts() == {"succeeded": 1}
    assert orch.result("solved").metadata["flexorch_user"] == "ana"
    orch.delete_study("s")  # owner
    assert orch.studies() == []


@pytest.mark.parametrize(
    ("client", "headers"),
    [
        (("192.168.1.20", 50000), {}),  # another machine
        (("127.0.0.1", 50000), {"Host": "evil.example"}),  # DNS rebinding
        (("127.0.0.1", 50000), {"Origin": "https://evil.example"}),  # a website
        (("127.0.0.1", 50000), {"Origin": "null"}),  # a file:// page
    ],
)
def test_local_mode_refuses_anything_but_this_machine(client, headers):
    with TestClient(local_app(), base_url=LOCAL, client=client) as c:
        assert c.get("/studies", headers=headers).status_code == 403
        assert c.get("/health", headers=headers).status_code == 200


def test_local_mode_serves_local_pages():
    with TestClient(local_app(), base_url=LOCAL, client=("::1", 50000)) as c:
        for origin in (
            "http://localhost:3000",
            "http://127.0.0.1",
            "http://[::1]:5173",
        ):
            assert c.get("/studies", headers={"Origin": origin}).status_code == 200
        assert c.get("/studies", headers={"Host": "localhost:8080"}).status_code == 200


def test_local_mode_takes_no_tokens():
    tokens = parse_api_tokens(f"bob:owner:{hash_token('t')}", "local")
    with pytest.raises(ValueError, match="no tokens"):
        Settings(tenant="local", local_user="ana", api_tokens=tokens)
    assert Settings.local().local_user  # the logged-in user


def test_serve_local_binds_loopback_only(capsys):
    assert cli.main(["serve", "--local", "--host", "0.0.0.0"]) == 1
    assert "loopback only" in capsys.readouterr().err


def test_no_token_is_refused_where_tokens_are_needed():
    with TestClient(make_test_app()) as client:
        with pytest.raises(AccessError, match="No token") as refused:
            remote(client, "http://tenant.test").studies()
    assert refused.value.status == 401


# A spec's files over HTTP


def test_a_spec_with_files_is_stored_as_from_python(tmp_path):
    (tmp_path / "tariff.csv").write_text("hour,price\n0,0.1\n")
    spec = {"value": 1.0, "tariff": "tariff.csv"}
    app = make_test_app()
    with TestClient(app) as client:
        orch = remote(client, "http://tenant.test", token_for("editor"))
        orch.create_study("s")
        sent = orch.store_model_spec(
            "base", spec, base_dir=tmp_path, assets=["/tariff"]
        )
        child = orch.store_model_spec(
            "child", {**sent.spec, "value": 2.0}, parent="base", assets=["/tariff"]
        )
    local = app.state.orchestrator
    local.create_study("direct")
    direct = local.store_model_spec("base", spec, base_dir=tmp_path, assets=["/tariff"])
    assert sent.spec == direct.spec and sent.spec_hash == direct.spec_hash
    assert sent.spec["tariff"].startswith("assets/") and sent.spec["tariff"].endswith(
        "-tariff.csv"
    )
    assert child.spec["tariff"] == sent.spec["tariff"]  # the parent's file


@pytest.mark.parametrize(
    "name", ["../x.csv", "0123456789ab/../x", "0123456789ab/.", "x/t.csv", "t.csv"]
)
def test_spec_file_names_cant_leave_the_scratch_folder(name):
    with TestClient(make_test_app()) as client:
        headers = {"Authorization": f"Bearer {token_for('editor')}"}
        client.post("/studies", json={"name": "s"}, headers=headers)
        found = client.post(
            "/studies/s/specs/model/base",
            json={"spec": {"value": 1.0}, "files": {name: "eA=="}},
            headers=headers,
        )
    assert found.status_code == 422
    assert found.json()["error"] == SpecError.__name__
