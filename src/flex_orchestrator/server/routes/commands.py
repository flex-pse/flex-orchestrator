"""Commands and jobs (PLAN.md §4, D18).

Every command, built-in or a plugin's, is reachable as ``POST
/commands/{name}`` with its parameters as the JSON body, checked against its
params model and its ``role``. A ``long_running`` command answers ``202
{"job_id"}`` and runs on the tenant's job queue; any other answers with its
result. ``execute_study`` and ``fit_surrogate`` also have study routes that
go the same way.
"""

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.responses import JSONResponse

from flex_orchestrator.ext.commands import to_json
from flex_orchestrator.server.auth import Principal
from flex_orchestrator.server.deps import (
    current_principal,
    get_jobs,
    get_orchestrator,
    require_role,
    study_orchestrator,
)

router = APIRouter(tags=["commands"])


@router.get("/commands", dependencies=[Depends(require_role("viewer"))])
def list_commands(orch: Any = Depends(get_orchestrator)) -> dict:
    """Every command: built-in and the enabled plugins', with its parameters' schema."""
    return {
        "commands": [
            {
                "name": cmd.name,
                "description": cmd.description,
                "role": cmd.role,
                "long_running": cmd.long_running,
                "source": orch.commands.source(cmd.name),
                "params_schema": cmd.params_model.model_json_schema(),
            }
            for cmd in orch.commands
        ]
    }


@router.post("/commands/{name}")
def run_command(
    name: str,
    params: dict[str, Any] = Body(default_factory=dict),
    study: str | None = Query(None, description="the study to select first"),
    principal: Principal = Depends(current_principal),
    orch: Any = Depends(get_orchestrator),
    jobs: Any = Depends(get_jobs),
) -> Any:
    """Run a command: ``202 {"job_id"}`` if it's long-running, else its result.

    Its ``role`` decides who may (403 otherwise); its params model checks the
    body (422).
    """
    cmd = orch.commands.get(name)
    if not principal.allows(cmd.role):
        raise HTTPException(
            403, f"{name} needs the {cmd.role} role; the token has {principal.role}."
        )
    if study is not None:
        orch.select_study(study)
    if cmd.long_running:
        return _accepted(jobs.submit(name, params, study=study, user=principal.user))
    return {"result": to_json(orch.run_command(name, params))}


@router.post("/studies/{study}/execute", dependencies=[Depends(require_role("editor"))])
def execute_study(
    study: str,
    config: dict[str, Any] = Body(...),
    principal: Principal = Depends(current_principal),
    orch: Any = Depends(study_orchestrator),
    jobs: Any = Depends(get_jobs),
) -> JSONResponse:
    """Run a study config in ``study``: ``202 {"job_id"}``."""
    return _accepted(
        jobs.submit(
            "execute_study", {"config": config}, study=study, user=principal.user
        )
    )


@router.post(
    "/studies/{study}/surrogates/fit", dependencies=[Depends(require_role("editor"))]
)
def fit_surrogate(
    study: str,
    params: dict[str, Any] = Body(...),
    principal: Principal = Depends(current_principal),
    orch: Any = Depends(study_orchestrator),
    jobs: Any = Depends(get_jobs),
) -> JSONResponse:
    """``POST /commands/fit_surrogate?study=...``, by another name (D19)."""
    return _accepted(
        jobs.submit("fit_surrogate", params, study=study, user=principal.user)
    )


@router.get("/jobs/{job_id}", dependencies=[Depends(require_role("viewer"))])
def get_job(job_id: str, jobs: Any = Depends(get_jobs)) -> dict:
    """A job's status, progress, result or error; 404 if there's none."""
    return jobs.get(job_id)


@router.delete("/jobs/{job_id}", dependencies=[Depends(require_role("editor"))])
def cancel_job(job_id: str, jobs: Any = Depends(get_jobs)) -> dict:
    """Cancel a queued or running job; a finished one is left as it is."""
    return jobs.cancel(job_id)


def _accepted(job: Any) -> JSONResponse:
    view = job.view()
    return JSONResponse(
        {"job_id": job.id, "status": view["status"], "url": f"/jobs/{job.id}"},
        status_code=202,
        headers={"Location": f"/jobs/{job.id}"},
    )
