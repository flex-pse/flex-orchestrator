"""Stopping an idle API (``FLEXORCH_IDLE_MINUTES``, PLAN.md M8).

A tenant can be persistent (the default: the API runs until someone stops
it) or time out: with ``FLEXORCH_IDLE_MINUTES`` set, the API stops itself
once that long has passed with no request and no job queued or running.
``/health`` and ``/ready`` don't count as activity, so health checks don't
keep it awake. In a tenant stack, ``idle_tenants.sh`` then stops the
tenant's Acquirium too, and ``start_tenant.sh`` starts both again.
"""

import logging
import os
import signal
import threading
import time
from collections.abc import Callable

_log = logging.getLogger("flex_orchestrator.server")

QUIET_PATHS = ("/health", "/ready")
"""tuple[str, ...]: Requests that don't count as activity."""


def stop_this_process() -> None:
    """Ask the server process to shut down, as ``docker stop`` would."""
    os.kill(os.getpid(), signal.SIGTERM)


class IdleWatch:
    """Calls ``on_idle`` once nothing has happened for ``minutes``.

    Args:
        minutes: How long to wait with no activity.
        busy: Whether work is under way (a job queued or running); while it
            is, the API isn't idle.
        on_idle: What to do then; :func:`stop_this_process` by default.
        clock: Seconds, monotonic (tests pass their own).
        check_every: Seconds between checks.
    """

    def __init__(
        self,
        minutes: float,
        busy: Callable[[], bool],
        on_idle: Callable[[], None] = stop_this_process,
        *,
        clock: Callable[[], float] = time.monotonic,
        check_every: float = 15.0,
    ) -> None:
        """Start counting from now; :meth:`start` begins checking."""
        self.seconds = minutes * 60
        self._busy = busy
        self._on_idle = on_idle
        self._clock = clock
        self._check_every = check_every
        self._last = clock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def touch(self) -> None:
        """Record activity: a request came in."""
        self._last = self._clock()

    def idle(self) -> bool:
        """Whether it's been idle long enough (work under way resets it)."""
        if self._busy():
            self.touch()
            return False
        return self._clock() - self._last >= self.seconds

    def start(self) -> None:
        """Check in a thread until idle (then call ``on_idle``) or stopped."""
        self._thread = threading.Thread(
            target=self._watch, name="flexorch-idle", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop checking."""
        self._stop.set()

    def _watch(self) -> None:
        while not self._stop.wait(self._check_every):
            if self.idle():
                _log.warning(
                    "No requests for %.0f minutes: stopping (FLEXORCH_IDLE_MINUTES).",
                    self.seconds / 60,
                )
                self._on_idle()
                return
