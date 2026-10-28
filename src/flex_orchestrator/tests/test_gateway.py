"""Tenants behind the gateway (PLAN.md M8): isolation, a generated client,
idle tenants and backups that are off.

Opt-in like ``test_deploy.py`` (``FLEXORCH_TEST_DEPLOY``), on the same
cluster. The gateway is (re)started on test ports, ``FLEXORCH_TEST_HTTPS_PORT``
(18443) and ``FLEXORCH_TEST_HTTP_PORT`` (18080), with its generated CA; four
fresh tenants are provisioned and removed afterwards.

The generated-client test generates a Python client from
``docs/schemas/openapi.json`` with ``openapi-python-client``: on ``PATH``, or
at ``FLEXORCH_OPENAPI_GENERATOR``.
"""

import importlib
import json
import os
import shlex
import shutil
import ssl
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from flex_orchestrator.tests.test_deploy import REPO, SETTING, _linux, inspect, sh

httpx = pytest.importorskip("httpx")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.timeout(3600),
    pytest.mark.skipif(not SETTING, reason="set FLEXORCH_TEST_DEPLOY to run"),
]

HTTPS = int(os.environ.get("FLEXORCH_TEST_HTTPS_PORT", "18443"))
HTTP = int(os.environ.get("FLEXORCH_TEST_HTTP_PORT", "18080"))
GATEWAY = f"https://localhost:{HTTPS}"
PORTS = (
    f"export FLEXORCH_GATEWAY_HTTPS_PORT={HTTPS} FLEXORCH_GATEWAY_HTTP_PORT={HTTP}\n"
)
UI = "https://ui.test"
COST = "costing.aggregate_operating_cost"


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    """Four tenants behind the gateway, each with an owner's and a viewer's token.

    ``a``, ``b`` and ``c`` are persistent (``c`` serves the UI origin); ``idle``
    times out after a minute and isn't backed up.
    """
    sh(
        'docker image inspect "$FLEXORCH_IMAGE" >/dev/null 2>&1 || docker build '
        '-f ../docker/Dockerfile -t "$FLEXORCH_IMAGE" ../.. >/dev/null'
    )
    sh("bash init_cluster.sh >/dev/null 2>&1")
    sh(PORTS + "bash ../gateway/init_gateway.sh >/dev/null 2>&1")
    key = uuid.uuid4().hex[:6]
    names = {k: f"gw_{k}_{key}" for k in ("a", "b", "c", "idle")}
    options = {
        "c": f"--cors-origins {UI}",
        "idle": "--idle-minutes 1 --tenant-backups off",
    }
    ca = tmp_path_factory.mktemp("gateway") / "ca.crt"
    ca.write_text(sh('cat "$GATEWAY_DIR/ca.crt"'))
    try:
        tokens = {}
        for k, tenant in names.items():
            extra = options.get(k, "")
            sh(f"bash provision_tenant.sh {tenant} {extra} >/dev/null 2>&1")
            tokens[k] = {
                role: sh(
                    f"bash api_token.sh {tenant} {k}_{role} {role} 2>/dev/null"
                ).strip()
                for role in ("owner", "viewer")
            }
            sh(f"bash start_tenant.sh {tenant} >/dev/null 2>&1")
        yield {"names": names, "tokens": tokens, "ca": str(ca)}
    finally:
        for tenant in names.values():
            sh(f"bash deprovision_tenant.sh {tenant} >/dev/null 2>&1", check=False)


def client(stack, tenant=None, token=None, timeout=60):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    base = f"{GATEWAY}/{tenant}" if tenant else GATEWAY
    return httpx.Client(
        base_url=base, verify=stack["ca"], headers=headers, timeout=timeout
    )


# Isolation behind the gateway


def test_each_tenant_answers_only_its_own_tokens(stack):
    names, tokens = stack["names"], stack["tokens"]
    for k in ("a", "b", "c"):
        with client(stack, names[k], tokens[k]["viewer"]) as own:
            assert own.get("/studies").json() == {"studies": []}
            assert own.get("/health").json()["tenant"] == names[k]
        for other in ("a", "b", "c"):
            if other != k:
                with client(stack, names[other], tokens[k]["owner"]) as wrong:
                    assert wrong.get("/studies").status_code == 401
    with client(stack, names["a"]) as nobody:
        assert nobody.get("/studies").status_code == 401
        assert nobody.get("/health").status_code == 200  # needs no token
    with client(stack, names["a"], "junk") as junk:
        assert junk.get("/studies").status_code == 401
    with client(stack, names["a"], tokens["a"]["viewer"]) as viewer:
        assert viewer.post("/studies", json={"name": "x"}).status_code == 403
    with client(stack, names["a"], tokens["a"]["owner"]) as owner:
        assert owner.post("/studies", json={"name": "x"}).status_code == 201
        assert owner.delete("/studies/x").status_code == 204


def test_the_gateway_routes_only_known_tenants(stack):
    a, b = stack["names"]["a"], stack["names"]["b"]
    with client(stack) as anyone:
        assert anyone.get("/nobody/health").status_code == 404
        assert anyone.get("/").status_code == 404
        # A path that climbs out of one tenant's prefix doesn't reach another.
        found = anyone.get(
            f"/{a}/..%2f{b}/studies",
            headers={"Authorization": f"Bearer {stack['tokens']['b']['owner']}"},
        )
        assert found.status_code in (401, 404)


def test_https_only_with_tls_1_2_or_later(stack):
    a = stack["names"]["a"]
    with httpx.Client(timeout=30) as plain:
        found = plain.get(f"http://localhost:{HTTP}/{a}/health")
    assert found.status_code in (301, 308)
    assert found.headers["location"] == f"https://localhost:{HTTPS}/{a}/health"
    with client(stack, a) as secure:
        with secure.stream("GET", "/health") as found:
            tls = found.extensions["network_stream"].get_extra_info("ssl_object")
            assert tls.version() in ("TLSv1.2", "TLSv1.3")
            assert found.headers["strict-transport-security"].startswith("max-age=")
    old = ssl.create_default_context(cafile=stack["ca"])
    old.maximum_version = ssl.TLSVersion.TLSv1_1
    with pytest.raises((httpx.ConnectError, ssl.SSLError)):
        httpx.get(f"{GATEWAY}/{a}/health", verify=old)


def test_the_gateway_reaches_apis_only(stack):
    names = stack["names"]
    networks = inspect(
        "flexorch-gateway",
        "{{range $name, $_ := .NetworkSettings.Networks}}{{$name}} {{end}}",
    ).split()
    assert "flexorch_gateway" in networks
    assert {f"flexorch_{t}_edge" for t in names.values()} <= set(networks)
    assert not [n for n in networks if n.endswith("_db") or n == "flexorch_cluster"]
    assert not [
        n for n in networks if n.startswith("flexorch_gw_") and not n.endswith("_edge")
    ]
    for tenant in names.values():
        acquirium = sh(f"tenant_compose {tenant} ps -q acquirium").strip()
        name = inspect(acquirium, "{{.Name}}").lstrip("/")
        for host in (name, "acquirium", "flexorch-pg"):
            probe = sh(
                f"docker exec flexorch-gateway wget -q -T 3 -O- http://{host}:8000/health"
                " 2>&1 || true"
            )
            assert "bad address" in probe or "timed out" in probe, probe
        on = inspect(
            acquirium,
            "{{range $name, $_ := .NetworkSettings.Networks}}{{$name}} {{end}}",
        )
        assert "_edge" not in on  # Acquirium is never on an edge network
    hardened = inspect(
        "flexorch-gateway",
        "{{.Config.User}} {{.HostConfig.ReadonlyRootfs}} {{json .HostConfig.CapDrop}}",
    )
    assert hardened == '1000:1000 true ["ALL"]'
    mounts = inspect("flexorch-gateway", "{{range .Mounts}}{{.Source}} {{end}}")
    assert "docker.sock" not in mounts


def test_cors_for_a_tenant_with_a_ui_origin(stack):
    names = stack["names"]
    preflight = {
        "Origin": UI,
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "authorization,content-type",
    }
    with client(stack, names["c"]) as c:
        found = c.options("/studies", headers=preflight)
        assert found.headers.get("access-control-allow-origin") == UI
    with client(stack, names["a"]) as a:  # no UI origin for a
        found = a.options("/studies", headers=preflight)
        assert "access-control-allow-origin" not in found.headers


# A UI-like client, generated from the published schema, runs scripts 3 and 4

PREPARE = r"""
import base64, json, sys
sys.path.insert(0, "/examples/ro_study")
import common
from sweep_surrogates import STUDY
import flexops as fo
from flex_orchestrator.assets import ASSET_DIR, collect_assets, find_assets, set_pointer
from flex_orchestrator.specs import detect_format, to_json_spec

spec = to_json_spec(fo.emit_model(common.ro_model(), relative_to=common.MODEL))
found = find_assets(spec, detect_format(spec))
spec, refs, read = collect_assets(spec, found, common.MODEL)
files = {}
for pointer, ref in refs.items():
    digest, _, name = ref.name.removeprefix(f"{ASSET_DIR}/").partition("-")
    set_pointer(spec, pointer, f"{digest}/{name}")
    files[f"{digest}/{name}"] = base64.b64encode(read[ref.name]).decode()
data = common.operating_data()
print(json.dumps({
    "spec": spec, "files": files, "study": STUDY,
    "data": {"time_index": [t.isoformat() for t in data.index],
             "columns": {c: {"unit": common.UNITS[c], "values": data[c].tolist()}
                         for c in data.columns}},
}))
"""


@pytest.fixture(scope="module")
def sdk(tmp_path_factory):
    """A client generated from docs/schemas/openapi.json, imported."""
    generator = os.environ.get("FLEXORCH_OPENAPI_GENERATOR") or shutil.which(
        "openapi-python-client"
    )
    if not generator:
        pytest.skip("needs openapi-python-client (or FLEXORCH_OPENAPI_GENERATOR)")
    folder = tmp_path_factory.mktemp("sdk")
    done = subprocess.run(
        [generator, "generate", "--meta", "none", "--output-path", "flexorch_sdk",
         "--path", str(REPO / "docs" / "schemas" / "openapi.json")],
        cwd=folder, capture_output=True, text=True, timeout=300,
    )  # fmt: skip
    assert done.returncode == 0, done.stdout + done.stderr
    sys.path.insert(0, str(folder))
    try:
        yield importlib.import_module("flexorch_sdk")
    finally:
        sys.path.remove(str(folder))


def follow(stack, tenant, token, job_id):
    """Read the job's progress stream through the gateway; return its last state."""
    events = []
    with client(stack, tenant, token, timeout=None) as c:
        with c.stream("GET", f"/jobs/{job_id}/events") as stream:
            assert stream.headers["content-type"].startswith("text/event-stream")
            for line in stream.iter_lines():
                if line.startswith("event: "):
                    events.append(line.removeprefix("event: "))
                elif line.startswith("data: ") and events[-1] == "done":
                    return events, json.loads(line.removeprefix("data: "))
    raise AssertionError(f"the stream ended without done: {events}")


def test_a_generated_client_runs_scripts_3_and_4(stack, sdk):
    from flexorch_sdk import AuthenticatedClient
    from flexorch_sdk.api.commands import execute_study, get_job
    from flexorch_sdk.api.studies import (
        create_study,
        list_results,
        store_data,
        store_spec,
    )
    from flexorch_sdk.models import DataIn, ExecuteStudyConfig, NewStudy, SpecIn
    from flexorch_sdk.models.store_spec_kind import StoreSpecKind

    from flex_orchestrator.testing import DEMO_SPEC

    tenant, token = stack["names"]["b"], stack["tokens"]["b"]["owner"]
    api = AuthenticatedClient(
        base_url=f"{GATEWAY}/{tenant}",
        token=token,
        verify_ssl=stack["ca"],
        timeout=httpx.Timeout(120),
    )

    def run(study, config):
        found = execute_study.sync_detailed(
            study, client=api, body=ExecuteStudyConfig.from_dict(config)
        )
        assert found.status_code == 202, found.content
        job = json.loads(found.content)["job_id"]
        events, last = follow(stack, tenant, token, job)
        assert last["status"] == "succeeded", last
        assert get_job.sync_detailed(job, client=api).status_code == 200
        return events

    def costs(study):
        page = list_results.sync(study, client=api, limit=50).to_dict()
        return {
            tag: found["scalars"][COST]["value"]
            for tag, found in page["results"].items()
        }

    # Script 4: the smoke study, its pump flow set from stored data.
    assert (
        create_study.sync_detailed(client=api, body=NewStudy(name="smoke")).status_code
        == 201
    )
    start = datetime(2025, 7, 7, tzinfo=UTC)
    hours = [(start + timedelta(hours=h)).isoformat() for h in range(48)]
    flow = {"unit": "m^3/hr", "values": [3.0 + h / 8 for h in range(48)]}
    body = {"time_index": hours, "columns": {"flow": flow}}
    assert (
        store_data.sync_detailed(
            "smoke", "pump", client=api, body=DataIn.from_dict(body)
        ).status_code
        == 201
    )
    spec = SpecIn.from_dict({"spec": DEMO_SPEC})
    assert (
        store_spec.sync_detailed(
            "smoke", StoreSpecKind.MODEL, "demo", client=api, body=spec
        ).status_code
        == 201
    )
    pump = {
        "dataset": "pump",
        "columns": ["flow"],
        "rename": {"flow": "demo.pump.flow_in"},
    }
    run("smoke", {
        "base_model_spec": "demo",
        "sweeps": [{"type": "model_inputs", "name": "day", "values": [
            {"label": "today", "inputs": [pump]},
            {"label": "yesterday", "inputs": [{**pump, "offset": "-1D"}]}]}],
        "export": {"include": ["demo.pump.flow_in", COST]},
        "output_name": "{day}",
    })  # fmt: skip
    smoke = costs("smoke")
    assert set(smoke) == {"today", "yesterday"} and smoke["today"] > smoke["yesterday"]

    # Script 3: the RO surrogate sweep, its spec's tariff file sent with it.
    prepared = json.loads(
        sh(
            f"tenant_compose {tenant} run --rm -T "
            f"-v {shlex.quote(_linux(REPO / 'examples'))}:/examples:ro orchestrator "
            f"python -c {shlex.quote(PREPARE)} 2>/dev/null | tail -1"
        )
    )
    assert (
        create_study.sync_detailed(client=api, body=NewStudy(name="ro")).status_code
        == 201
    )
    assert (
        store_data.sync_detailed(
            "ro", "ro_operation", client=api, body=DataIn.from_dict(prepared["data"])
        ).status_code
        == 201
    )
    spec = SpecIn.from_dict({"spec": prepared["spec"], "files": prepared["files"]})
    stored = store_spec.sync("ro", StoreSpecKind.MODEL, "base", client=api, body=spec)
    tariffs = [
        v
        for v in json.dumps(stored.spec.to_dict()).split('"')
        if v.startswith("assets/")
    ]
    assert tariffs and tariffs[0].endswith("-tariff.csv")
    events = run("ro", prepared["study"])
    assert events[-1] == "done"
    ro = costs("ro")
    assert ro == pytest.approx(
        {"fixed_intensity": 1285.96, "linear_surrogate": 1238.92}, abs=0.01
    )


# Idle tenants


def running(tenant, service):
    profile = "--profile api " if service == "api" else ""
    return bool(
        sh(f"tenant_compose {tenant} {profile}ps -q --status running {service}").strip()
    )


def test_an_idle_tenant_stops_and_starts_again(stack):
    tenant, token = stack["names"]["idle"], stack["tokens"]["idle"]["owner"]
    # The earlier tests took over a minute, so it may well be asleep already.
    sh(f"bash start_tenant.sh {tenant} >/dev/null 2>&1")
    with client(stack, tenant, token) as c:
        assert c.post("/studies", json={"name": "kept"}).status_code == 201
    assert running(tenant, "api") and running(tenant, "acquirium")
    persistent = stack["names"]["a"]
    deadline = time.monotonic() + 240
    while running(tenant, "api"):  # a minute with no requests, then its check
        assert time.monotonic() < deadline, "the idle API didn't stop"
        time.sleep(10)
    with client(stack, tenant, token) as c:
        assert c.get("/studies").status_code in (502, 503)  # asleep
    sh("bash idle_tenants.sh >/dev/null 2>&1")
    assert not running(tenant, "acquirium")
    assert running(persistent, "acquirium") and running(persistent, "api")  # untouched
    sh(f"bash start_tenant.sh {tenant} >/dev/null 2>&1")
    with client(stack, tenant, token) as c:
        assert c.get("/studies").json() == {"studies": ["kept"]}


# Backups that are off


def test_backups_can_be_off(stack):
    idle, a = stack["names"]["idle"], stack["names"]["a"]
    # A tenant that opted out isn't backed up, even when asked directly.
    out = sh(f"bash backup_tenant.sh {idle} 2>&1")
    assert "backups are off" in out
    assert (
        sh(f"s3 s3 ls s3://$BUCKET/tenants/{idle}/ 2>/dev/null || true").strip() == ""
    )
    # The whole cluster off: the scripts do nothing, restore refuses.
    off = "export FLEXORCH_BACKUPS=off\n"
    assert "backups are off" in sh(off + f"bash backup_tenant.sh {a} 2>&1")
    assert "nothing to do" in sh(off + "bash backup_cluster.sh 2>&1")
    refused = sh(
        off + f"bash restore_tenant.sh {a} now --into x_{a} 2>&1 || echo rc=$?"
    )
    assert "backups are off" in refused and "rc=1" in refused
    # And the cluster it would run: no S3 server, no WAL archiving.
    config = sh(off + "cluster_compose config")
    assert "archive_mode=off" in config and "flexorch-s3" not in config
    assert "archive_mode=on" in sh("cluster_compose config")  # the cluster's own: on


# The example that sets this up from Python


def test_the_docker_tenants_example(stack):
    pytest.importorskip("flexparameterize")
    env = {
        **os.environ,
        "FLEXORCH_DOCKER_SHELL": "" if SETTING == "1" else SETTING,
        "FLEXORCH_DEMO_HTTPS_PORT": str(HTTPS),
        "FLEXORCH_DEMO_HTTP_PORT": str(HTTP),
    }
    example = REPO / "examples" / "server_examples" / "docker_tenants.py"
    done = subprocess.run(
        [sys.executable, str(example)],
        capture_output=True,
        text=True,
        timeout=1800,
        env=env,
    )
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-3000:]
    for line in (
        "fixed_intensity: $1,285.96",
        "linear_surrogate: $1,238.92",
        "vic deletes a result: refused (403)",
        "carol in demo_a: refused (401)",
        "carol's studies in demo_b: []",
        "5. removing the tenants",
    ):
        assert line in done.stdout
