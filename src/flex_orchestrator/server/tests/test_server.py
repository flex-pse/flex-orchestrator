"""The tenant's HTTP API (PLAN.md §4, D15, M7), in-process.

FastAPI's TestClient against a memory store, with JWTs signed by the test key
(``flex_orchestrator.server.testing``): checked exactly as an identity
provider's would be.
"""

import io
import json
import time
import zipfile

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from flex_orchestrator.backend import MemoryBackend
from flex_orchestrator.ext import Plugin, command
from flex_orchestrator.server import Principal, Settings, hash_token
from flex_orchestrator.server.jobs import JOBS_LEDGER
from flex_orchestrator.server.settings import parse_api_tokens
from flex_orchestrator.server.testing import make_test_app, token_for

pytestmark = pytest.mark.unit

HOOKS = {
    "build": "flex_orchestrator.study.tests.ray_hooks:build",
    "solve": "flex_orchestrator.study.tests.ray_hooks:solve",
}


class Echo(BaseModel):
    text: str


@command(role="viewer")
def echo(orch, params: Echo) -> dict:
    """Say it back, and who asked."""
    return {"text": params.text, "user": orch.user}


@command(role="owner")
def wipe(orch) -> str:
    """An owner-only command."""
    return "wiped"


@command(long_running=True)
def slow(orch, params: Echo, progress) -> str:
    """Wait until cancelled, or half a second."""
    progress.update(total=50)
    for _ in range(50):
        progress.check()
        time.sleep(0.01)
        progress.advance()
    return params.text


PLUGIN = Plugin(name="probe", commands=[echo, wipe, slow])


def auth(role="editor", tenant="test", **claims):
    return {"Authorization": f"Bearer {token_for(role, tenant, **claims)}"}


@pytest.fixture
def backend():
    return MemoryBackend()


@pytest.fixture
def client(backend):
    with TestClient(make_test_app(plugins=[PLUGIN], backend=backend)) as client:
        yield client


@pytest.fixture
def study(client):
    assert (
        client.post("/studies", json={"name": "s"}, headers=auth()).status_code == 201
    )
    hours = pd.date_range("2025-07-08", periods=24, freq="h", tz="UTC")
    q = np.linspace(10, 50, 24)
    body = {
        "time_index": [t.isoformat() for t in hours],
        "columns": {
            "q": {"unit": "m^3/hr", "values": list(q)},
            "p": {"unit": "kW", "values": list(3 * q + 1)},
        },
    }
    assert (
        client.post("/studies/s/data/ops", json=body, headers=auth()).status_code == 201
    )
    spec = {"spec": {"value": 1.0}}
    assert (
        client.post(
            "/studies/s/specs/model/base", json=spec, headers=auth()
        ).status_code
        == 201
    )
    return "s"


def wait(client, job_id, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = client.get(f"/jobs/{job_id}", headers=auth("viewer")).json()
        if found["status"] not in ("queued", "running"):
            return found
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} didn't finish: {found}")


# Authentication (D15)


def test_health_and_ready_need_no_token(client):
    assert client.get("/health").json() == {
        "status": "ok",
        "tenant": "test",
        "api": "1",
    }
    assert client.get("/ready").json() == {"status": "ready"}


@pytest.mark.parametrize(
    ("headers", "status"),
    [
        ({}, 401),
        ({"Authorization": "Basic dXNlcjpwdw=="}, 401),
        ({"Authorization": "Bearer not-a-jwt"}, 401),
        (auth(expires_in=-60), 401),
        (auth(aud="someone-else"), 401),
        (auth(iss="https://evil.example"), 401),
        (auth(tenant="other"), 403),
        (auth(role="admin"), 403),
    ],
)
def test_bad_credentials(client, headers, status):
    found = client.get("/studies", headers=headers)
    assert found.status_code == status, found.text
    if status == 401:
        assert found.headers["www-authenticate"] == "Bearer"


def test_a_token_signed_by_another_key_is_refused(client):
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa

    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    claims = jwt.decode(token_for(), options={"verify_signature": False})
    forged = jwt.encode(
        claims, other, algorithm="RS256", headers={"kid": "flexorch-test"}
    )
    assert (
        client.get(
            "/studies", headers={"Authorization": f"Bearer {forged}"}
        ).status_code
        == 401
    )
    unsigned = jwt.encode(claims, key=None, algorithm="none")
    assert (
        client.get(
            "/studies", headers={"Authorization": f"Bearer {unsigned}"}
        ).status_code
        == 401
    )


def test_static_tokens_without_an_identity_provider(backend):
    tokens = parse_api_tokens(
        f"alice:viewer:{hash_token('a-token')};bob:owner:{hash_token('b-token')}", "lab"
    )
    settings = Settings(
        tenant="lab", api_tokens=tokens, orchestrator={"backend": backend}, environ={}
    )
    from flex_orchestrator.server import create_app

    with TestClient(create_app(settings)) as client:
        assert (
            client.get(
                "/studies", headers={"Authorization": "Bearer a-token"}
            ).status_code
            == 200
        )
        assert (
            client.get("/studies", headers={"Authorization": "Bearer nope"}).status_code
            == 401
        )
        # alice is a viewer: she may read, not create.
        assert (
            client.post(
                "/studies",
                json={"name": "x"},
                headers={"Authorization": "Bearer a-token"},
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/studies",
                json={"name": "x"},
                headers={"Authorization": "Bearer b-token"},
            ).status_code
            == 201
        )
        # A JWT means nothing here: there's no identity provider.
        assert client.get("/studies", headers=auth("owner", "lab")).status_code == 401
    assert tokens[hash_token("a-token")] == Principal("alice", "lab", "viewer")


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({}, "tenant"),
        ({"FLEXORCH_TENANT": "t"}, "No way to authenticate"),
        ({"FLEXORCH_TENANT": "t", "FLEXORCH_OIDC_ISSUER": "https://i"}, "audience"),
        (
            {"FLEXORCH_TENANT": "t", "FLEXORCH_API_TOKENS": "alice:root:sha256:00"},
            "entry 1",
        ),
        (
            {"FLEXORCH_TENANT": "t", "FLEXORCH_API_TOKENS": "alice:viewer:plain"},
            "entry 1",
        ),
    ],
)
def test_settings_from_the_environment(env, message):
    with pytest.raises(ValueError, match=message):
        Settings.from_env(env)


def test_settings_never_repeat_a_token():
    with pytest.raises(ValueError) as found:
        Settings.from_env(
            {"FLEXORCH_TENANT": "t", "FLEXORCH_API_TOKENS": "u:viewer:s3cr3t-token"}
        )
    assert "s3cr3t" not in str(found.value)


def test_every_route_needs_a_token_for_this_tenant(client):
    """Walk every route the app serves: none answers without a valid token.

    The routes come from the app's OpenAPI schema, so a route added by a
    plugin is walked too.
    """
    open_paths = {"/health", "/ready"}
    checked = 0
    for path, operations in client.app.openapi()["paths"].items():
        if path in open_paths:
            continue
        url = path.replace("{", "").replace("}", "")
        for method in operations:
            for headers, status in ((None, 401), (auth("owner", "other"), 403)):
                found = client.request(method, url, headers=headers)
                assert found.status_code == status, (method, path, found.text)
            checked += 1
    assert checked >= 20


@pytest.mark.parametrize(
    ("method", "path", "body", "role", "status"),
    [
        ("post", "/studies", {"name": "x"}, "viewer", 403),
        ("post", "/studies/s/specs/model/x", {"spec": {"value": 2}}, "viewer", 403),
        ("delete", "/studies/s/data/ops", None, "editor", 403),
        ("delete", "/studies/s", None, "editor", 403),
        ("post", "/studies/s/execute", {}, "viewer", 403),
        ("delete", "/studies/s/data/ops", None, "owner", 204),
    ],
)
def test_roles(client, study, method, path, body, role, status):
    found = client.request(method, path, json=body, headers=auth(role))
    assert found.status_code == status, found.text


# Studies, specs, data, results


def test_errors_become_statuses(client, study):
    again = client.post("/studies", json={"name": "s"}, headers=auth())
    assert (again.status_code, again.json()["error"]) == (409, "StudyExistsError")
    assert client.get("/studies/nope", headers=auth()).status_code == 404
    assert client.get("/studies/s/specs/model/nope", headers=auth()).status_code == 404
    assert (
        client.post(
            "/studies/s/specs/model/base", json={"spec": {}}, headers=auth()
        ).status_code
        == 409
    )
    assert (
        client.get("/studies/s/data/ops?columns=zz", headers=auth()).status_code == 404
    )
    assert (
        client.post(
            "/studies/s/data/x", json={"columns": {}}, headers=auth()
        ).status_code
        == 422
    )
    assert (
        client.delete("/studies/s/oops/base", headers=auth("owner")).status_code == 422
    )


def test_a_study_specs_and_data(client, study):
    assert client.get("/studies", headers=auth("viewer")).json() == {"studies": ["s"]}
    items = client.get("/studies/s", headers=auth("viewer")).json()["items"]
    assert items["data"]["tags"] == ["ops"] and items["model_spec"]["tags"] == ["base"]
    spec = client.get("/studies/s/specs/model/base", headers=auth("viewer")).json()
    assert spec["spec"] == {"value": 1.0} and spec["kind"] == "model_spec"
    derived = client.post(
        "/studies/s/specs/model/derived",
        json={"spec": {"value": 2.0}, "parent": "base"},
        headers=auth(),
    ).json()
    assert derived["parent_tag"] == "base"
    assert client.get("/studies/s/specs/model", headers=auth()).json() == {
        "tags": ["base", "derived"]
    }
    data = client.get("/studies/s/data/ops?columns=q", headers=auth("viewer")).json()
    assert list(data["columns"]) == ["q"] and data["columns"]["q"]["unit"] == "m^3/hr"
    assert data["time_index"][0] == "2025-07-08T00:00:00+00:00"
    assert data["created_by"] == "tester"  # the token's user, not the server's


def test_results_are_the_export_files(client, backend, study, tmp_path):
    # A result stored from Python, read over HTTP: the same JSON export writes.
    orch = client.app.state.orchestrator
    orch.select_study("s")
    for tag in ("r1", "r2", "r3"):
        model = orch.build("base", build=HOOKS["build"], value=float(len(tag)))
        orch.store_model_results(
            tag, model, orch.registry.resolve_ref(HOOKS["solve"], "solve")(model)
        )
    page = client.get("/studies/s/results?limit=2", headers=auth("viewer")).json()
    assert (list(page["results"]), page["total"], page["next"]) == (["r1", "r2"], 3, 2)
    rest = client.get(
        "/studies/s/results?limit=2&offset=2", headers=auth("viewer")
    ).json()
    assert (list(rest["results"]), rest["next"]) == (["r3"], None)
    one = client.get("/studies/s/results/r1", headers=auth("viewer")).json()
    exported = json.loads(orch.export_result("r1", tmp_path / "r1.json").read_text())
    assert one == exported
    assert client.get("/studies/s/results/nope", headers=auth()).status_code == 404


# Commands and jobs


def test_the_command_list(client):
    found = {
        c["name"]: c
        for c in client.get("/commands", headers=auth("viewer")).json()["commands"]
    }
    assert {"execute_study", "fit_surrogate", "echo", "wipe", "slow"} <= set(found)
    assert (
        found["echo"]["source"] == "plugin 'probe'"
        and found["echo"]["role"] == "viewer"
    )
    assert found["execute_study"]["long_running"] is True
    assert "properties" in found["echo"]["params_schema"]


def test_a_command_runs_with_its_role(client):
    answer = client.post("/commands/echo", json={"text": "hi"}, headers=auth("viewer"))
    assert answer.json() == {"result": {"text": "hi", "user": "tester"}}
    assert client.post("/commands/wipe", headers=auth("editor")).status_code == 403
    assert client.post("/commands/wipe", headers=auth("owner")).json() == {
        "result": "wiped"
    }
    assert (
        client.post("/commands/echo", json={"txt": "x"}, headers=auth()).status_code
        == 422
    )
    assert client.post("/commands/nope", headers=auth()).status_code == 422


def test_a_long_command_is_a_job(client):
    accepted = client.post("/commands/slow", json={"text": "done"}, headers=auth())
    assert accepted.status_code == 202
    job_id = accepted.json()["job_id"]
    assert accepted.headers["location"] == f"/jobs/{job_id}"
    found = wait(client, job_id)
    assert (found["status"], found["result"], found["user"]) == (
        "succeeded",
        "done",
        "tester",
    )
    assert found["progress"]["done"] == 50
    assert client.get("/jobs/nope", headers=auth()).status_code == 404


def test_cancelling_a_job(client):
    first = client.post("/commands/slow", json={"text": "a"}, headers=auth()).json()[
        "job_id"
    ]
    second = client.post("/commands/slow", json={"text": "b"}, headers=auth()).json()[
        "job_id"
    ]
    client.delete(f"/jobs/{second}", headers=auth())  # still queued behind the first
    client.delete(f"/jobs/{first}", headers=auth())
    assert wait(client, first)["status"] == "cancelled"
    assert wait(client, second)["status"] == "cancelled"


def test_jobs_outlive_the_server(backend):
    with TestClient(make_test_app(plugins=[PLUGIN], backend=backend)) as client:
        job_id = client.post(
            "/commands/slow", json={"text": "kept"}, headers=auth()
        ).json()["job_id"]
        wait(client, job_id)
    # A job recorded as running when a server starts was cut short.
    backend.start_run(
        JOBS_LEDGER,
        {"flexorch_kind": "job", "flexorch_tag": "cut", "flexorch_command": "slow"},
    )
    with TestClient(make_test_app(plugins=[PLUGIN], backend=backend)) as client:
        kept = client.get(f"/jobs/{job_id}", headers=auth()).json()
        assert (kept["status"], kept["result"]) == ("succeeded", "kept")
        assert client.get("/jobs/cut", headers=auth()).json()["status"] == "interrupted"


def test_a_failing_job(client, study):
    accepted = client.post(
        "/studies/s/execute", json={"base_model_spec": "nope"}, headers=auth()
    )
    found = wait(client, accepted.json()["job_id"])
    assert found["status"] == "succeeded"  # the study ran; its task failed
    assert found["result"]["results"][0]["status"] == "error"
    bad = client.post("/studies/s/execute", json={"sweeps": "nope"}, headers=auth())
    assert bad.status_code == 422  # checked before it's queued


def test_http_and_python_store_the_same(client, study):
    config = {
        "base_model_spec": "base",
        "hooks": HOOKS,
        "sweeps": [{"type": "variable", "name": "v", "path": "p.v", "values": [1, 2]}],
        "export": {"exclude": ["pid", "threads"]},
    }
    over_http = client.post("/studies/s/execute", json=config, headers=auth())
    assert wait(client, over_http.json()["job_id"])["status"] == "succeeded"
    orch = client.app.state.orchestrator
    orch.create_study("python")
    orch.store_model_spec("base", {"value": 1.0})
    orch.execute_study(config)

    def stored(name):
        orch.select_study(name)
        return {r.tag: r.scalars() for r in orch.stored_model_results()}

    assert (
        stored("s") == stored("python") == {"base_1": {"x": 2.0}, "base_2": {"x": 3.0}}
    )
    fit = {
        "tag": "lin",
        "regressor": "multilinear",
        "X": {"dataset": "ops", "columns": ["q"]},
        "y": {"dataset": "ops", "columns": ["p"]},
    }
    job = client.post("/studies/s/surrogates/fit", json=fit, headers=auth()).json()[
        "job_id"
    ]
    assert wait(client, job)["status"] == "succeeded"
    same = client.post(
        "/commands/fit_surrogate?study=s", json={**fit, "tag": "lin2"}, headers=auth()
    )
    assert wait(client, same.json()["job_id"])["status"] == "succeeded"
    orch.select_study("s")
    first, second = orch.surrogate_spec("lin"), orch.surrogate_spec("lin2")
    assert first.spec["data"] == second.spec["data"]


# Export and import


def test_export_and_import_as_zips(client, study):
    exported = client.get("/studies/s/export", headers=auth("viewer"))
    assert exported.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
        assert "study.json" in archive.namelist()
    files = {"file": ("s.zip", exported.content, "application/zip")}
    accepted = client.post(
        "/studies/import", files=files, data={"name": "copy"}, headers=auth()
    )
    assert accepted.status_code == 202
    found = wait(client, accepted.json()["job_id"])
    assert found["status"] == "succeeded", found
    assert (
        found["result"]["counts"]["specs"] == 1 and found["result"]["study"] == "copy"
    )
    assert client.get("/studies/copy/data/ops", headers=auth()).status_code == 200


def test_unsafe_or_broken_uploads_are_refused(client):
    unsafe = io.BytesIO()
    with zipfile.ZipFile(unsafe, "w") as archive:
        archive.writestr("../escape.txt", "x")
    for content in (unsafe.getvalue(), b"not a zip"):
        found = client.post(
            "/studies/import", files={"file": ("x.zip", content)}, headers=auth()
        )
        assert found.status_code == 422, found.text


# Plugin routes (D18)


def plugin_with_routes(name="probe_routes", prefix=""):
    from fastapi import APIRouter, Depends

    from flex_orchestrator.server.deps import get_orchestrator, require_role

    router = APIRouter(prefix=prefix)

    # Its author added no authentication: the mount does.
    @router.get("/hello")
    def hello(orch=Depends(get_orchestrator)) -> dict:
        return {"hello": orch.user, "studies": orch.studies()}

    @router.post("/purge", dependencies=[Depends(require_role("owner"))])
    def purge() -> dict:
        return {"purged": True}

    return Plugin(name=name, routers=[router])


def test_plugin_routes_live_under_ext_with_auth(backend):
    app = make_test_app(plugins=[plugin_with_routes()], backend=backend)
    with TestClient(app) as client:
        assert client.get("/ext/probe_routes/hello").status_code == 401
        assert (
            client.get(
                "/ext/probe_routes/hello", headers=auth("viewer", "other")
            ).status_code
            == 403
        )
        assert client.get("/ext/probe_routes/hello", headers=auth("viewer")).json() == {
            "hello": "tester",
            "studies": [],
        }
        assert (
            client.post("/ext/probe_routes/purge", headers=auth("editor")).status_code
            == 403
        )
        assert client.post("/ext/probe_routes/purge", headers=auth("owner")).json() == {
            "purged": True
        }
        tags = {
            t
            for op in app.openapi()["paths"]["/ext/probe_routes/hello"].values()
            for t in op["tags"]
        }
        assert tags == {"plugin:probe_routes"}
        test_every_route_needs_a_token_for_this_tenant(client)  # the plugin's too


@pytest.mark.parametrize("prefix", ["/studies", "/jobs/x", "/ext"])
def test_a_plugin_cant_claim_a_core_prefix(backend, prefix):
    from flex_orchestrator.errors import PluginError

    with pytest.raises(PluginError, match="reserves"):
        make_test_app(plugins=[plugin_with_routes(prefix=prefix)], backend=backend)


def test_token_hashes_from_a_file(tmp_path):
    path = tmp_path / "api_tokens"
    path.write_text(f"alice:viewer:{hash_token('a')}\n\nbob:owner:{hash_token('b')}\n")
    settings = Settings.from_env(
        {"FLEXORCH_TENANT": "t", "FLEXORCH_API_TOKENS_FILE": str(path)}
    )
    assert {p.user for p in settings.api_tokens.values()} == {"alice", "bob"}
    with pytest.raises(ValueError, match="Can't read FLEXORCH_API_TOKENS_FILE"):
        Settings.from_env(
            {"FLEXORCH_TENANT": "t", "FLEXORCH_API_TOKENS_FILE": str(tmp_path / "no")}
        )
