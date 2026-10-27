"""Request dependencies: the public surface plugin routes use too (PLAN.md D18).

Core routes and plugin routers depend on the same functions::

    from fastapi import APIRouter, Depends
    from flex_orchestrator.server.deps import get_orchestrator, require_role

    router = APIRouter()

    @router.get("/summary", dependencies=[Depends(require_role("viewer"))])
    def summary(orch=Depends(get_orchestrator)):
        return {"studies": orch.studies()}

- :func:`current_principal`: the authenticated caller (401/403 otherwise).
- :func:`require_role`: a dependency refusing callers below a role (403).
- :func:`get_orchestrator`: this request's orchestrator: the tenant's, as the
  plugins' subclass if one provides it, with no study selected.
- :func:`study_orchestrator`: the same, with the path's ``{study}`` selected
  (404 if it doesn't exist).
- :func:`get_jobs`: the tenant's job queue.
- :func:`get_settings`: the server's settings.
"""

import copy
from collections.abc import Callable
from typing import Any

from fastapi import Depends, HTTPException, Request

from flex_orchestrator.server.auth import ROLES, AuthError, Principal


def get_settings(request: Request) -> Any:
    """Return the server's :class:`~flex_orchestrator.server.Settings`."""
    return request.app.state.settings


def current_principal(request: Request) -> Principal:
    """Return the caller, from the request's bearer token.

    Raises:
        HTTPException: 401 without a valid token; 403 for another tenant's.
    """
    found = getattr(request.state, "principal", None)
    if found is not None:
        return found
    try:
        principal = request.app.state.authenticator.authenticate(
            request.headers.get("authorization"),
            request.client.host if request.client else None,
            request.headers,
        )
    except AuthError as exc:
        headers = {"WWW-Authenticate": "Bearer"} if exc.status == 401 else None
        raise HTTPException(exc.status, str(exc), headers=headers) from None
    request.state.principal = principal
    return principal


def require_role(role: str) -> Callable[..., Principal]:
    """Return a dependency that admits callers with at least ``role``.

    Raises:
        ValueError: If ``role`` isn't a role.
    """
    if role not in ROLES:
        raise ValueError(f"role must be one of {list(ROLES)}, not {role!r}")

    def check(principal: Principal = Depends(current_principal)) -> Principal:
        if not principal.allows(role):
            raise HTTPException(
                403, f"This needs the {role} role; the token has {principal.role}."
            )
        return principal

    check.role = role  # for the route walk in the tests
    return check


def get_orchestrator(
    request: Request, principal: Principal = Depends(current_principal)
) -> Any:
    """Return this request's orchestrator, recording ``principal`` as its user.

    A shallow copy of the tenant's: it shares the store, hooks and commands,
    and has its own selected study, so requests don't change each other's.
    """
    orch = copy.copy(request.app.state.orchestrator)
    orch._study = None
    orch.user = principal.user
    return orch


def study_orchestrator(study: str, orch: Any = Depends(get_orchestrator)) -> Any:
    """Return this request's orchestrator with ``study`` (from the path) selected."""
    orch.select_study(study)
    return orch


def get_jobs(request: Request) -> Any:
    """Return the tenant's :class:`~flex_orchestrator.server.jobs.Jobs`."""
    return request.app.state.jobs
