"""A long-lived local Acquirium: ``flex-orch acquirium start | status | stop``.

A managed Acquirium (modes 1 and 2) lives only as long as the first process
that opened its ``data_dir``. One that runs its own drivers or data
processing has to outlive every script, so it's started on its own::

    flex-orch --data-dir .flexorch acquirium start      # prints its URL
    flex-orch --data-dir .flexorch acquirium status
    flex-orch --data-dir .flexorch acquirium stop

:func:`start` runs ``acquirium server`` detached, on loopback only (Acquirium
has no login of its own), and records it in the ``data_dir``
(``flexorch-service.json``). With ``config=``, it serves your own
``acquirium.toml``, drivers and all; otherwise it writes one with the managed
server's settings (DuckDB, exact matching), so the same ``data_dir`` holds the
same stores either way.

While it runs, ``Orchestrator(data_dir=...)`` and ``connect(data_dir=...)``
attach to it (:func:`find`) instead of starting their own, and never stop it.
Set ``FLEXORCH_START_ACQUIRIUM=0`` to make them fail rather than start a
short-lived one when it isn't running.

Only Acquirium's public interfaces are used: its ``server`` command, its
config file and ``GET /health``.
"""

import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from flex_orchestrator.errors import StorageError

STATE_NAME = "flexorch-service.json"
"""str: The record of a running service, in its ``data_dir``."""
CONFIG_NAME = "flexorch-service.toml"
"""str: The config :func:`start` writes when it isn't given one."""
LOG_NAME = "flexorch-service.log"
"""str: The service's output, in its ``data_dir``."""
HOST = "127.0.0.1"

# connection's DEFAULT_DATA_DIR and (mode 2) CONFIG_NAME; this module doesn't
# import it, so the CLI can manage a server without importing Acquirium.
_DEFAULT_DATA_DIR = ".flexorch"
_POSTGRES_CONFIG = "flexorch-acquirium.toml"

_LOCAL_CONFIG = """\
# Written by flex-orchestrator (flex-orch acquirium start): the settings of a
# managed local Acquirium, so this folder holds the same stores either way.
[server]
enabled = true
data_dir = "."
timeseries_backend = "duckdb"
workers = 1
exact_only = true
recreate = false
"""


@dataclass(frozen=True)
class Service:
    """A running long-lived Acquirium.

    Attributes:
        url: Where it listens (loopback).
        pid: Its process.
        data_dir: The folder it serves.
        config: The ``acquirium.toml`` it was started with.
        log: Its output.
    """

    url: str
    pid: int
    data_dir: str
    config: str
    log: str


def start(
    data_dir: str | Path | None = None,
    *,
    config: str | Path | None = None,
    port: int | None = None,
    timeout: float = 120.0,
) -> Service:
    """Start a long-lived Acquirium on ``data_dir``, or the one ``config`` names.

    Args:
        data_dir: The folder to serve; ``.flexorch`` by default. Not with
            ``config``, whose ``[server] data_dir`` is the folder.
        config: Your own ``acquirium.toml`` (drivers, apps, storage).
        port: The port; a free one by default.
        timeout: Seconds to wait for it to answer.

    Raises:
        ValueError: For both ``data_dir`` and ``config``, or a config with no
            ``data_dir``.
        StorageError: If one is already running there, or it didn't start (the
            message ends with its log).
    """
    if config is not None and data_dir is not None:
        raise ValueError("config= names its data_dir; don't pass both.")
    if config is not None:
        config = Path(config).expanduser().resolve()
        root = _config_data_dir(config)
    else:
        root = _root(data_dir)
        if (root / _POSTGRES_CONFIG).exists():
            raise StorageError(
                f"{root} is a Postgres-backed Acquirium's; start it with config= "
                "and the database login in its environment."
            )
    running = find(root)
    if running is not None:
        raise StorageError(f"An Acquirium is already running for {root}: {running.url}")
    root.mkdir(parents=True, exist_ok=True)
    port = port or _free_port()
    if config is None:
        config = root / CONFIG_NAME
        config.write_text(_LOCAL_CONFIG, encoding="utf-8")
    log = root / LOG_NAME
    # The config decides the storage, as for a managed server: inherited
    # ACQUIRIUM_* settings must not redirect it.
    env = {k: v for k, v in os.environ.items() if not k.startswith("ACQUIRIUM_")}
    command = [sys.executable, "-m", "acquirium.cli", "server", "--config",
               str(config), "--host", HOST, "--port", str(port)]  # fmt: skip
    with log.open("w", encoding="utf-8") as out:
        process = subprocess.Popen(
            command,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=subprocess.STDOUT,
            **_detached(),
        )
    service = Service(
        f"http://{HOST}:{port}", process.pid, str(root), str(config), str(log)
    )
    deadline = time.monotonic() + timeout
    while not _healthy(service.url):
        if process.poll() is not None or time.monotonic() > deadline:
            if process.poll() is None:
                process.kill()
            raise StorageError(
                f"The Acquirium for {root} didn't start; its log ({log}) ends:\n"
                + _tail(log)
            )
        time.sleep(0.2)
    (root / STATE_NAME).write_text(
        json.dumps(asdict(service), indent=2), encoding="utf-8"
    )
    return service


def find(data_dir: str | Path | None = None) -> Service | None:
    """Return the long-lived Acquirium running for ``data_dir``, if one answers."""
    path = _root(data_dir) / STATE_NAME
    try:
        service = Service(**json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        return None
    # Only ever contact loopback, whatever the file says.
    if not service.url.startswith(f"http://{HOST}:") or not _healthy(service.url):
        return None
    return service


def stop(data_dir: str | Path | None = None, timeout: float = 60.0) -> Service | None:
    """Stop the long-lived Acquirium for ``data_dir``; its data is kept.

    On Linux and macOS it's asked to stop (``SIGTERM``); Windows has no such
    signal for a detached process, so it's ended, as Acquirium's own managed
    server is.

    Returns:
        What was stopped, or ``None`` if nothing was running.

    Raises:
        StorageError: If it's still answering after ``timeout`` seconds.
    """
    root = _root(data_dir)
    service = find(root)
    if service is not None:
        try:
            os.kill(service.pid, signal.SIGTERM)
        except OSError:
            pass  # gone already
        deadline = time.monotonic() + timeout
        while _healthy(service.url):
            if time.monotonic() > deadline:
                raise StorageError(f"The Acquirium at {service.url} didn't stop.")
            time.sleep(0.2)
    (root / STATE_NAME).unlink(missing_ok=True)
    return service


def status(data_dir: str | Path | None = None) -> dict[str, Any]:
    """Return ``{"running": bool, ...the service}`` for ``data_dir``."""
    service = find(data_dir)
    if service is None:
        return {"running": False, "data_dir": str(_root(data_dir))}
    return {"running": True, **asdict(service)}


def _root(data_dir: str | Path | None) -> Path:
    return Path(data_dir or _DEFAULT_DATA_DIR).expanduser().resolve()


def _config_data_dir(config: Path) -> Path:
    import tomllib

    try:
        with config.open("rb") as file:
            data_dir = tomllib.load(file).get("server", {}).get("data_dir")
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"Can't read {config}: {exc}") from None
    if not data_dir:
        raise ValueError(f"{config} has no [server] data_dir.")
    return (config.parent / Path(data_dir).expanduser()).resolve()


def _healthy(url: str) -> bool:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"{url}/health", timeout=1) as response:
            return json.load(response).get("ok") is True
    except (OSError, ValueError, urllib.error.URLError):
        return False


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind((HOST, 0))
        return probe.getsockname()[1]


def _detached() -> dict[str, Any]:
    """Popen options for a process that outlives this one and its terminal."""
    if sys.platform == "win32":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        return {"creationflags": flags}
    return {"start_new_session": True}


def _tail(path: Path, lines: int = 20) -> str:
    try:
        return "\n".join(
            path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]
        )
    except OSError:
        return "(no log)"
