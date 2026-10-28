"""The long-lived local Acquirium (``flex-orch acquirium``) and attaching to it."""

import json
import os
import subprocess
import sys

import pytest

from flex_orchestrator import cli, service
from flex_orchestrator.connection import connect
from flex_orchestrator.errors import StorageError
from flex_orchestrator.service import STATE_NAME, Service

unit = pytest.mark.unit


@pytest.fixture
def aq(monkeypatch):
    """A fake ``acquirium.init``; records each call."""
    acquirium = pytest.importorskip("acquirium")
    calls = []
    monkeypatch.setattr(acquirium, "init", lambda **kw: calls.append(kw) or "client")
    monkeypatch.setattr(acquirium, "shutdown", lambda: calls.append("shutdown"))
    return calls


def running(tmp_path, url="http://127.0.0.1:8123"):
    return Service(url, 42, str(tmp_path), "c.toml", "l.log")


@unit
def test_a_data_dir_with_a_long_lived_server_attaches_to_it(aq, tmp_path, monkeypatch):
    monkeypatch.setattr(service, "find", lambda root: running(tmp_path))
    conn = connect(data_dir=tmp_path)
    assert aq == [{"address": "http://127.0.0.1:8123", "timeout": 600.0}]
    assert (conn.mode, conn.target, conn.owned) == ("local", str(tmp_path), False)


@unit
@pytest.mark.parametrize("value", ["0", "false", "no", "OFF"])
def test_start_acquirium_off_refuses_to_start_one(aq, tmp_path, value):
    with pytest.raises(StorageError, match="acquirium start"):
        connect(data_dir=tmp_path, environ={"FLEXORCH_START_ACQUIRIUM": value})
    with pytest.raises(StorageError, match="acquirium start"):
        connect(
            data_dir=tmp_path,
            db="postgresql://u@h/db",
            environ={"FLEXORCH_START_ACQUIRIUM": value},
        )
    assert aq == []


@unit
def test_start_acquirium_on_starts_a_managed_one(aq, tmp_path):
    conn = connect(data_dir=tmp_path, environ={"FLEXORCH_START_ACQUIRIUM": "1"})
    assert conn.owned and aq[0]["data_dir"] == tmp_path


@unit
def test_find_trusts_only_a_loopback_server_that_answers(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "_healthy", lambda url: True)
    assert service.find(tmp_path) is None  # no record
    (tmp_path / STATE_NAME).write_text("not json")
    assert service.find(tmp_path) is None
    for url, found in [("http://evil.example:80", False), ("http://127.0.0.1:9", True)]:
        record = {**running(tmp_path, url).__dict__}
        (tmp_path / STATE_NAME).write_text(json.dumps(record))
        assert (service.find(tmp_path) is not None) is found
    monkeypatch.setattr(service, "_healthy", lambda url: False)  # stale
    assert service.find(tmp_path) is None
    assert service.status(tmp_path) == {"running": False, "data_dir": str(tmp_path)}


@unit
def test_start_needs_one_place_to_serve(tmp_path):
    config = tmp_path / "acquirium.toml"
    with pytest.raises(ValueError, match="don't pass both"):
        service.start(tmp_path, config=config)
    config.write_text("[server]\nport = 1\n")
    with pytest.raises(ValueError, match="no \\[server\\] data_dir"):
        service.start(config=config)
    (tmp_path / "flexorch-acquirium.toml").write_text("")
    with pytest.raises(StorageError, match="Postgres-backed"):
        service.start(tmp_path)


@unit
def test_the_service_names_match_the_connections():
    from flex_orchestrator import connection

    assert service._DEFAULT_DATA_DIR == connection.DEFAULT_DATA_DIR
    assert service._POSTGRES_CONFIG == connection.CONFIG_NAME


@unit
def test_the_cli_serves_data_dirs_only(capsys):
    assert cli.main(["--acquirium-url", "http://x:1", "acquirium", "status"]) == 1
    assert "--data-dir" in capsys.readouterr().err


# A real one

ATTACH = """
import json, sys
from flex_orchestrator import Orchestrator

with Orchestrator(data_dir=sys.argv[1]) as orch:
    if sys.argv[2] == "create":
        orch.create_study("kept")
    print(json.dumps({"studies": orch.studies(), "owned": orch.connection.owned}))
"""


def flex_orch(*args, env=None):
    done = subprocess.run(
        [sys.executable, "-m", "flex_orchestrator", *args],
        capture_output=True,
        text=True,
        timeout=300,
        env=env,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    return json.loads(done.stdout)


@pytest.mark.integration
def test_a_long_lived_server_outlives_the_scripts_that_use_it(tmp_path):
    pytest.importorskip("acquirium")
    store = str(tmp_path / "store")
    env = {**os.environ, "FLEXORCH_START_ACQUIRIUM": "0"}

    def attach(step):
        done = subprocess.run(
            [sys.executable, "-c", ATTACH, store, step],
            capture_output=True,
            text=True,
            timeout=300,
            env=env,
        )
        assert done.returncode == 0, done.stdout + done.stderr
        return json.loads(done.stdout.splitlines()[-1])

    started = flex_orch("--data-dir", store, "acquirium", "start")
    try:
        assert started["url"].startswith("http://127.0.0.1:")
        # Each script attaches, and leaves it running when it exits.
        assert attach("create") == {"studies": ["kept"], "owned": False}
        assert flex_orch("--data-dir", store, "acquirium", "status")["running"]
        assert attach("read") == {"studies": ["kept"], "owned": False}
    finally:
        assert flex_orch("--data-dir", store, "acquirium", "stop") == {"stopped": True}
    assert flex_orch("--data-dir", store, "acquirium", "status")["running"] is False
    # With it stopped and starting forbidden, a script fails instead.
    done = subprocess.run(
        [sys.executable, "-c", ATTACH, store, "read"],
        capture_output=True,
        text=True,
        timeout=300,
        env=env,
    )
    assert done.returncode != 0 and "acquirium start" in done.stderr
