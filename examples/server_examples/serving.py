"""What the server examples share: a server in a thread, and the RO study.

``start_server(settings)`` serves a fresh store (``tmp_flexorch`` here) in a
thread of this process and stops it cleanly, so its Acquirium stops too. The
study is the ``ro_study`` surrogate sweep, unchanged: ``new_study`` stores its
data and spec, ``STUDY`` is its config.
"""

import dataclasses
import socket
import sys
import threading
from pathlib import Path

import uvicorn

from flex_orchestrator.server import Settings, create_app

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent / "ro_study"))
from common import fresh_store, new_study, report  # noqa: E402, F401
from sweep_surrogates import STUDY  # noqa: E402, F401


def start_server(
    settings: Settings | None = None,
) -> tuple[uvicorn.Server, threading.Thread, str]:
    """Serve a fresh store in a thread; wait until it's up.

    With ``settings`` (the store is added), else in local mode. Stop it with
    ``server.should_exit = True`` and ``thread.join()``.

    Returns:
        The server, its thread, and its URL.
    """
    store = {"data_dir": fresh_store(HERE)}
    if settings is None:
        settings = Settings.local(orchestrator=store)
    else:
        settings = dataclasses.replace(settings, orchestrator=store)
    with socket.socket() as probe:  # a free port
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    config = uvicorn.Config(create_app(settings), host="127.0.0.1", port=port)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run)
    thread.start()
    while not server.started and thread.is_alive():
        thread.join(0.1)
    return server, thread, f"http://127.0.0.1:{port}"
