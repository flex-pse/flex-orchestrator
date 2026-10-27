"""``flex-orch serve`` and ``flex-orch login`` (PLAN.md M7)."""

import io
import json

import pytest

from flex_orchestrator.cli import main
from flex_orchestrator.server import hash_token

pytestmark = pytest.mark.unit


def test_login_saves_a_token_from_stdin(tmp_path, monkeypatch, capsys):
    target = tmp_path / "token"
    monkeypatch.setattr("flex_orchestrator.client.TOKEN_FILE", target)
    monkeypatch.setattr("sys.stdin", io.StringIO("my-token\n"))
    assert main(["login"]) == 0
    assert json.loads(capsys.readouterr().out) == {"saved": str(target)}
    assert target.read_text() == "my-token\n"
    monkeypatch.setattr("sys.stdin", io.StringIO("\n"))
    assert main(["login"]) == 1
    assert "no token" in capsys.readouterr().err


def test_login_checks_the_token_against_the_server(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("flex_orchestrator.client.TOKEN_FILE", tmp_path / "token")
    monkeypatch.setattr("sys.stdin", io.StringIO("my-token\n"))
    monkeypatch.setattr(
        "flex_orchestrator.client.RemoteOrchestrator.studies",
        lambda self: (_ for _ in ()).throw(
            __import__("flex_orchestrator.errors").errors.AccessError("401: no", 401)
        ),
    )
    assert main(["login", "--url", "http://tenant.test"]) == 1
    assert "AccessError" in capsys.readouterr().err
    assert not (tmp_path / "token").exists()  # nothing saved


def test_serve_builds_the_app_from_the_environment(tmp_path, monkeypatch, capsys):
    served = {}
    monkeypatch.setattr(
        "uvicorn.run", lambda app, host, port: served.update(app=app, port=port)
    )
    monkeypatch.setenv("FLEXORCH_TENANT", "lab")
    monkeypatch.setenv("FLEXORCH_API_TOKENS", f"alice:owner:{hash_token('t')}")
    monkeypatch.delenv("FLEXORCH_PLUGINS", raising=False)
    assert main(["--files", str(tmp_path / "store"), "serve", "--port", "9999"]) == 0
    app = served["app"]
    assert served["port"] == 9999
    assert app.state.settings.tenant == "lab"
    assert type(app.state.orchestrator.backend).__name__ == "FileBackend"
    monkeypatch.delenv("FLEXORCH_TENANT")
    assert main(["--files", str(tmp_path / "store"), "serve"]) == 1
    assert "FLEXORCH_TENANT" in capsys.readouterr().err
