"""The control plane contract (PLAN.md D16, M8): CORS, progress streaming,
the idle timeout, the path prefix, and the published OpenAPI schema."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from flex_orchestrator.backend import MemoryBackend
from flex_orchestrator.server import Settings, create_app
from flex_orchestrator.server.idle import IdleWatch
from flex_orchestrator.server.openapi import SCHEMA_PATH, core_schema, dumps
from flex_orchestrator.server.testing import make_test_app, token_for
from flex_orchestrator.server.tests.test_server import HOOKS, PLUGIN
from flex_orchestrator.testing.orchestrators import FAKE_HOOKS

pytestmark = pytest.mark.unit

UI = "https://ui.example.org"


def auth(role="editor"):
    return {"Authorization": f"Bearer {token_for(role)}"}


# CORS


def test_cors_lets_only_the_ui_origin_read_responses():
    with TestClient(make_test_app(cors_origins=(UI,))) as client:
        allowed = client.get("/studies", headers={"Origin": UI, **auth()})
        assert allowed.headers["access-control-allow-origin"] == UI
        other = client.get(
            "/studies", headers={"Origin": "https://evil.example", **auth()}
        )
        assert "access-control-allow-origin" not in other.headers
        # The browser's preflight needs no token.
        preflight = client.options(
            "/studies",
            headers={
                "Origin": UI,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization,content-type",
            },
        )
        assert preflight.status_code == 200
        allowed_headers = preflight.headers["access-control-allow-headers"].lower()
        assert "authorization" in allowed_headers
        assert "access-control-allow-credentials" not in preflight.headers


def test_no_cors_unless_asked():
    with TestClient(make_test_app()) as client:
        found = client.get("/studies", headers={"Origin": UI, **auth()})
        assert "access-control-allow-origin" not in found.headers


def test_local_mode_lets_local_pages_read_responses():
    app = create_app(
        Settings.local(
            "ana", orchestrator={"backend": MemoryBackend(), "hooks": FAKE_HOOKS}
        )
    )
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 1)) as c:
        found = c.get("/studies", headers={"Origin": "http://localhost:3000"})
        assert found.headers["access-control-allow-origin"] == "http://localhost:3000"


@pytest.mark.parametrize("origin", ["*", "ui.example.org", "ftp://ui"])
def test_cors_origins_must_be_origins(origin):
    with pytest.raises(ValueError, match="FLEXORCH_CORS_ORIGINS"):
        Settings.local(cors_origins=(origin,))


def test_the_new_settings_come_from_the_environment():
    settings = Settings.from_env(
        {
            "FLEXORCH_TENANT": "lab",
            "FLEXORCH_API_TOKENS": "",
            "FLEXORCH_OIDC_ISSUER": "https://idp",
            "FLEXORCH_OIDC_AUDIENCE": "a",
            "FLEXORCH_CORS_ORIGINS": f"{UI}, http://localhost:5173",
            "FLEXORCH_IDLE_MINUTES": "30",
            "FLEXORCH_ROOT_PATH": "/lab",
        }
    )
    assert settings.cors_origins == (UI, "http://localhost:5173")
    assert (settings.idle_minutes, settings.root_path) == (30.0, "/lab")
    for name, value in [
        ("FLEXORCH_IDLE_MINUTES", "soon"),
        ("FLEXORCH_IDLE_MINUTES", "0"),
        ("FLEXORCH_ROOT_PATH", "lab/"),
    ]:
        with pytest.raises(ValueError, match=name):
            Settings.from_env(
                {"FLEXORCH_TENANT": "lab", "FLEXORCH_OIDC_ISSUER": "https://idp",
                 "FLEXORCH_OIDC_AUDIENCE": "a", name: value}
            )  # fmt: skip


# Progress as Server-Sent Events


def events(text):
    found = []
    for block in text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        found.append((lines["event"], json.loads(lines["data"])))
    return found


def test_a_jobs_progress_streams_until_it_finishes():
    with TestClient(make_test_app(plugins=[PLUGIN])) as client:
        client.post("/studies", json={"name": "s"}, headers=auth())
        client.post(
            "/studies/s/specs/model/base",
            json={"spec": {"value": 1.0}},
            headers=auth(),
        )
        job = client.post(
            "/studies/s/execute",
            json={"base_model_spec": "base", "hooks": HOOKS},
            headers=auth(),
        ).json()["job_id"]
        with client.stream(
            "GET", f"/jobs/{job}/events", headers=auth("viewer")
        ) as stream:
            assert stream.headers["content-type"].startswith("text/event-stream")
            found = events(stream.read().decode())
        assert found[-1][0] == "done" and found[-1][1]["status"] == "succeeded"
        assert all(name == "progress" for name, _ in found[:-1])
        assert client.get(f"/jobs/{job}/events").status_code == 401
        assert client.get("/jobs/nope/events", headers=auth()).status_code == 404


# The idle timeout


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_idle_counts_from_the_last_request_and_waits_for_jobs():
    clock, busy = Clock(), [False]
    watch = IdleWatch(10, lambda: busy[0], lambda: None, clock=clock)
    clock.now = 599
    assert not watch.idle()
    watch.touch()
    clock.now = 1198
    assert not watch.idle()
    clock.now = 1199
    assert watch.idle()
    busy[0] = True  # a job running: not idle, and the count starts again
    assert not watch.idle()
    busy[0] = False
    clock.now = 1200
    assert not watch.idle()


def test_requests_but_health_checks_keep_the_api_awake():
    app = make_test_app(idle_minutes=5)
    with TestClient(app) as client:
        watch = app.state.idle
        watch._last = -1e9
        client.get("/health")
        client.get("/ready")
        assert watch.idle()
        client.get("/studies", headers=auth())
        assert not watch.idle()
    # Behind a gateway, the health check's path has the prefix in front.
    app = make_test_app(idle_minutes=5)
    with TestClient(app, root_path="/tenant_a") as client:
        watch = app.state.idle
        watch._last = -1e9
        assert client.get("/tenant_a/health").status_code == 200
        assert watch.idle()
        client.get("/tenant_a/studies", headers=auth())
        assert not watch.idle()


def test_the_watch_stops_the_server_once_idle():
    stopped = []
    watch = IdleWatch(0.0001, lambda: False, lambda: stopped.append(1),
                      check_every=0.01)  # fmt: skip
    watch.start()
    watch._thread.join(timeout=5)
    assert stopped == [1]


# Behind a gateway, and the published schema


def test_behind_a_gateway_paths_work_with_and_without_the_prefix():
    # The gateway strips /tenant_a, and the server's root path (what flex-orch
    # serve gives uvicorn) puts it back in front of the path.
    with TestClient(make_test_app(), root_path="/tenant_a") as client:
        schema = client.get("/tenant_a/openapi.json").json()
        assert schema["servers"] == [{"url": "/tenant_a"}]
        assert "/tenant_a/openapi.json" in client.get("/tenant_a/docs").text
        assert client.get("/tenant_a/studies", headers=auth()).status_code == 200
        assert client.get("/health").status_code == 200  # the health check's way


def test_the_published_schema_is_current():
    path = Path(__file__).parents[4] / SCHEMA_PATH
    assert path.read_text(encoding="utf-8") == dumps(core_schema()), (
        "The routes changed: run flex-orch openapi --output " + SCHEMA_PATH
    )


def test_operation_ids_are_the_route_names_and_unique():
    from fastapi import APIRouter

    from flex_orchestrator.ext import Plugin

    router = APIRouter()

    @router.get("/health")
    def health() -> dict:  # the same name as a core route's
        return {}

    app = make_test_app(plugins=[Plugin(name="probe", routers=[router])])
    ids = [
        op["operationId"]
        for path in app.openapi()["paths"].values()
        for op in path.values()
    ]
    assert len(ids) == len(set(ids))
    assert {"store_spec", "execute_study", "health", "probe_health"} <= set(ids)


def test_the_schema_declares_the_bearer_token():
    schema = core_schema()
    assert schema["components"]["securitySchemes"]["bearer"]["scheme"] == "bearer"
    assert schema["security"] == [{"bearer": []}]
    assert schema["paths"]["/health"]["get"]["security"] == []
    assert "/jobs/{job_id}/events" in schema["paths"]
