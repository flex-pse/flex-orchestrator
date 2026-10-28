"""The tenant's job queue (PLAN.md §4, D18): long-running commands, one at a time.

``POST /commands/{name}`` for a ``long_running`` command (``execute_study``,
``fit_surrogate``, an import) answers ``202 {"job_id"}`` and queues the call;
a worker thread runs the queue in order with the tenant's orchestrator. A job
is ``queued``, ``running``, then ``succeeded``, ``failed`` or ``cancelled``.

Every job is recorded in ``flexorch::jobs`` (its command, parameters, caller,
study and result), so ``GET /jobs/{id}`` answers after a restart too. A job
still recorded as running when the server starts was cut short: it is marked
failed and reported ``interrupted``.
"""

import copy
import queue
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from flex_orchestrator.errors import CommandCancelled, TagNotFoundError
from flex_orchestrator.ext.commands import Progress, to_json

JOBS_LEDGER = "flexorch::jobs"
"""str: Where jobs are recorded."""
JOB_KIND = "job"
_PARAMS, _RESULT = "params", "result"


class Interrupted(Exception):  # noqa: N818 (recorded as the run's error type)
    """Recorded as the error of a job the server stopped while running."""


@dataclass
class Job:
    """One queued command call.

    Attributes:
        id: The job id.
        command: The command's name.
        params: Its validated parameters, as JSON.
        study: The study selected for it, if any.
        user: Who submitted it.
        status: ``queued``, ``running``, ``succeeded``, ``failed``,
            ``cancelled`` or (after a restart) ``interrupted``.
        submitted_at: When it was queued.
        progress: Its progress and cancel switch.
        result: What the command returned, as JSON.
        error: The error, for ``failed``.
        call: What runs instead of the command, for work that isn't one (an
            upload's import): ``call(orch, progress)``.
    """

    id: str
    command: str
    params: dict[str, Any]
    study: str | None
    user: str
    status: str = "queued"
    submitted_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    progress: Progress = field(default_factory=Progress)
    result: Any = None
    error: str | None = None
    run_id: str | None = None
    call: Callable[[Any, Progress], Any] | None = field(default=None, repr=False)

    def view(self) -> dict[str, Any]:
        """The job as ``GET /jobs/{id}`` returns it."""
        return {
            "job_id": self.id,
            "command": self.command,
            "study": self.study,
            "user": self.user,
            "status": self.status,
            "submitted_at": self.submitted_at,
            "progress": self.progress.snapshot(),
            "result": self.result,
            "error": self.error,
        }


class Jobs:
    """The queue and its worker; one per app.

    Args:
        orch: The tenant's orchestrator. Each job runs on a copy of it, so the
            study it selects doesn't change anyone else's.
    """

    def __init__(self, orch: Any) -> None:
        """Record interrupted jobs, then start the worker."""
        self._orch = orch
        self._jobs: dict[str, Job] = {}
        self._queue: queue.Queue[Job | None] = queue.Queue()
        self._lock = threading.Lock()
        backend = orch.backend
        backend.define_study(JOBS_LEDGER)
        backend.declare(JOBS_LEDGER, _PARAMS, role="input", kind="json")
        backend.declare(JOBS_LEDGER, _RESULT, role="output", kind="json")
        for run in backend.runs(JOBS_LEDGER, status="running"):
            backend.fail_run(run.run_id, Interrupted("The server stopped."))
        self._worker = threading.Thread(
            target=self._work, name="flexorch-jobs", daemon=True
        )
        self._worker.start()

    def submit(self, command: str, params: Any, *, study: str | None, user: str) -> Job:
        """Queue a command call and return its job.

        Raises:
            CommandError: If there's no such command or the parameters don't
                validate (checked now, not when it runs).
        """
        validated = self._orch.commands.get(command).validate(params)
        return self._queue_job(
            Job(
                id=uuid.uuid4().hex,
                command=command,
                params=validated.model_dump(mode="json"),
                study=study,
                user=user,
            )
        )

    def submit_call(
        self,
        name: str,
        call: Callable[[Any, Progress], Any],
        params: dict[str, Any],
        *,
        study: str | None,
        user: str,
    ) -> Job:
        """Queue work that isn't a command, as ``name``; ``call(orch, progress)``."""
        return self._queue_job(
            Job(
                id=uuid.uuid4().hex,
                command=name,
                params=params,
                study=study,
                user=user,
                call=call,
            )
        )

    def _queue_job(self, job: Job) -> Job:
        backend = self._orch.backend
        job.run_id = backend.start_run(
            JOBS_LEDGER,
            {
                "flexorch_kind": JOB_KIND,
                "flexorch_tag": job.id,
                "flexorch_command": job.command,
                "flexorch_study": job.study,
                "flexorch_user": job.user,
                "submitted_at": job.submitted_at,
            },
        )
        backend.record(job.run_id, _PARAMS, job.params)
        with self._lock:
            self._jobs[job.id] = job
        self._queue.put(job)
        return job

    def get(self, job_id: str) -> dict[str, Any]:
        """Return a job's view, from this process or the ledger.

        Raises:
            TagNotFoundError: If there's no such job.
        """
        with self._lock:
            job = self._jobs.get(job_id)
        if job is not None:
            return job.view()
        backend = self._orch.backend
        found = backend.runs(JOBS_LEDGER, metadata={"flexorch_tag": job_id})
        if not found:
            raise TagNotFoundError(f"There is no job {job_id!r}.")
        run = found[-1]
        records = {r.label: r.value for r in backend.records(JOBS_LEDGER, run.run_id)}
        error = run.error or {}
        status = run.status
        if status == "failed" and error.get("type") == Interrupted.__name__:
            status = "interrupted"
        elif status == "failed" and error.get("type") == CommandCancelled.__name__:
            status = "cancelled"
        return {
            "job_id": job_id,
            "command": run.metadata.get("flexorch_command"),
            "study": run.metadata.get("flexorch_study"),
            "user": run.metadata.get("flexorch_user"),
            "status": status,
            "submitted_at": run.metadata.get("submitted_at"),
            "progress": None,
            "result": records.get(_RESULT),
            "error": error.get("message"),
        }

    def busy(self) -> bool:
        """Whether a job is queued or running in this process."""
        with self._lock:
            return any(j.status in ("queued", "running") for j in self._jobs.values())

    def cancel(self, job_id: str) -> dict[str, Any]:
        """Cancel a queued or running job; a finished one is left as it is.

        Raises:
            TagNotFoundError: If there's no such job in this process.
        """
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            self.get(job_id)  # 404 if unknown; otherwise it's already over
            return self.get(job_id)
        job.progress.cancel()
        return job.view()

    def close(self) -> None:
        """Cancel what's running and stop the worker (the app's shutdown)."""
        with self._lock:
            for job in self._jobs.values():
                job.progress.cancel()
        self._queue.put(None)
        self._worker.join(timeout=30)

    def _work(self) -> None:
        while True:
            job = self._queue.get()
            if job is None:
                return
            self._run(job)

    def _run(self, job: Job) -> None:
        backend = self._orch.backend
        if job.progress.cancelled:
            job.status = "cancelled"
            backend.fail_run(job.run_id, CommandCancelled("Cancelled while queued."))
            return
        job.status = "running"
        orch = copy.copy(self._orch)
        orch._study = None
        orch.user = job.user
        try:
            if job.study is not None:
                orch.select_study(job.study)
            if job.call is not None:
                result = job.call(orch, job.progress)
            else:
                result = orch.run_command(
                    job.command, job.params, progress=job.progress
                )
            job.result = to_json(result)
            backend.record(job.run_id, _RESULT, job.result)
            backend.finish_run(job.run_id)
            job.status = "succeeded"
        except CommandCancelled as exc:
            job.status, job.error = "cancelled", str(exc)
            backend.fail_run(job.run_id, exc)
        except Exception as exc:  # the job's failure is its result
            job.status, job.error = "failed", f"{type(exc).__name__}: {exc}"
            backend.fail_run(job.run_id, exc)
