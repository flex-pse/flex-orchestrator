"""``create_app``: the tenant's HTTP API (PLAN.md §4, M7).

There is no module-level app: build one with :func:`create_app` (uvicorn's
``--factory``, or ``flex-orch serve``). Every route but ``/health`` and
``/ready`` needs a bearer token for this tenant (D15); each route names the
least role it takes.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from flex_orchestrator import errors
from flex_orchestrator.server.auth import Authenticator
from flex_orchestrator.server.idle import QUIET_PATHS, IdleWatch
from flex_orchestrator.server.jobs import Jobs
from flex_orchestrator.server.settings import Settings

API_VERSION = "1"
"""str: The HTTP API's major version, in ``/health`` and the OpenAPI info."""

# Errors from the library, as HTTP statuses. The message goes in ``detail``.
_STATUS = [
    ((errors.StudyExistsError, errors.TagExistsError), 409),
    ((errors.StudyNotFoundError, errors.TagNotFoundError), 404),
    (
        (
            errors.SpecError,
            errors.StudyConfigError,
            errors.FitError,
            errors.ModelInputError,
            errors.CommandError,
            errors.ArchiveError,
            errors.UnitError,
            errors.ExportError,
            errors.HookError,
            errors.AmbiguousColumnError,
        ),
        422,
    ),
    ((errors.StorageError,), 503),
]


def create_app(settings: Settings | None = None, plugins: Any = None) -> FastAPI:
    """Build the tenant's app.

    Args:
        settings: The server's settings; :meth:`Settings.from_env` if ``None``.
        plugins: Plugins to load besides ``settings.plugins`` and
            ``FLEXORCH_PLUGINS`` (D18).

    Returns:
        The app. Its orchestrator connects when the app is built, as
        ``Orchestrator()`` does (``FLEXORCH_ACQUIRIUM_URL`` in a tenant stack),
        unless ``settings.orchestrator`` says otherwise.
    """
    from flex_orchestrator.ext import create_orchestrator
    from flex_orchestrator.server import extensions
    from flex_orchestrator.server.routes import archive, commands, studies

    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if app.state.idle is not None:
            app.state.idle.start()
        yield
        if app.state.idle is not None:
            app.state.idle.stop()
        app.state.jobs.close()
        app.state.orchestrator.close()  # stops a managed Acquirium it started

    app = FastAPI(
        title=f"flex-orchestrator ({settings.tenant})",
        version=API_VERSION,
        description="Studies, specs, data, results and commands of one tenant.",
        lifespan=lifespan,
        # Each route's function name is its operationId, so a client generated
        # from the schema has store_spec(), not store_spec_studies_..._post().
        generate_unique_id_function=lambda route: route.name,
    )
    app.state.settings = settings
    app.state.authenticator = Authenticator(settings)
    app.state.orchestrator = create_orchestrator(
        plugins=[*settings.plugins, *(plugins or [])],
        environ=settings.environ,
        **settings.orchestrator,
    )
    app.state.jobs = Jobs(app.state.orchestrator)
    app.state.idle = None
    if settings.idle_minutes is not None:
        app.state.idle = IdleWatch(settings.idle_minutes, app.state.jobs.busy)

        @app.middleware("http")
        async def activity(request: Request, call_next: Any) -> Any:
            # The path as routed: behind a gateway, the server puts its root
            # path in front of every request's, health checks' included.
            root = request.scope.get("root_path", "")
            if request.url.path.removeprefix(root) not in QUIET_PATHS:
                app.state.idle.touch()
            return await call_next(request)

    _cors(app, settings)
    _errors(app)
    _core(app)
    for routes in (studies, commands, archive):
        app.include_router(routes.router)
    extensions.mount(app, app.state.orchestrator.plugins)
    _bearer_in_schema(app)
    return app


def _bearer_in_schema(app: FastAPI) -> None:
    """Declare the bearer token in the OpenAPI schema, for clients and ``/docs``.

    Every route needs one but the health checks; in local mode none does.
    """
    build = app.openapi

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is None:
            schema = build()
            if app.state.settings.local_user is None:
                schema.setdefault("components", {})["securitySchemes"] = {
                    "bearer": {"type": "http", "scheme": "bearer"}
                }
                schema["security"] = [{"bearer": []}]
                for path in ("/health", "/ready"):
                    schema["paths"][path]["get"]["security"] = []
            app.openapi_schema = schema
        return app.openapi_schema

    app.openapi = openapi  # type: ignore[method-assign]


def _cors(app: FastAPI, settings: Settings) -> None:
    """Let pages from the allowed origins call the API (bearer tokens, no cookies)."""
    local = settings.local_user is not None
    if not settings.cors_origins and not local:
        return
    from fastapi.middleware.cors import CORSMiddleware

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        # In local mode, a local app's page on any loopback port too.
        allow_origin_regex=(
            r"https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?" if local else None
        ),
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["Authorization", "Content-Type"],
        expose_headers=["Location"],
        max_age=600,
    )


def _errors(app: FastAPI) -> None:
    for classes, status in _STATUS:
        for cls in classes:
            app.add_exception_handler(cls, _handler(status))


def _handler(status: int) -> Any:
    def handle(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(
            {"detail": str(exc), "error": type(exc).__name__}, status_code=status
        )

    return handle


def _core(app: FastAPI) -> None:
    @app.get("/health", tags=["health"])
    def health() -> dict:
        """Liveness: the process answers. Needs no token."""
        return {"status": "ok", "tenant": app.state.settings.tenant, "api": API_VERSION}

    @app.get("/ready", tags=["health"])
    def ready() -> JSONResponse:
        """Readiness: the store answers. Needs no token."""
        try:
            app.state.orchestrator.backend.studies()
        except Exception as exc:  # any failure to reach the store is "not ready"
            return JSONResponse(
                {"status": "unavailable", "detail": type(exc).__name__},
                status_code=503,
            )
        return JSONResponse({"status": "ready"})
