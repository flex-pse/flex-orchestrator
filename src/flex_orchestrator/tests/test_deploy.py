"""The tenant stacks (PLAN.md D14, M6): the isolation suite and the restore drill.

Opt-in, since they need Docker: set ``FLEXORCH_TEST_DEPLOY=1`` where ``bash``
and ``docker`` run, or to a command prefix that reaches them, e.g.
``FLEXORCH_TEST_DEPLOY="wsl.exe -d Ubuntu --"`` on Windows. The image
(``FLEXORCH_IMAGE``, ``flexorch:dev`` by default) is built if it's missing.

Two fresh tenants are provisioned on the cluster (started if needed) and
removed afterwards; the cluster itself is left running. Each step is a bash
script fed to ``bash -s`` with ``deploy/tenant/lib.sh`` sourced.
"""

import json
import os
import re
import shlex
import subprocess
import uuid
from pathlib import Path

import pytest

SETTING = os.environ.get("FLEXORCH_TEST_DEPLOY", "")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.timeout(3600),
    pytest.mark.skipif(not SETTING, reason="set FLEXORCH_TEST_DEPLOY to run"),
]

REPO = Path(__file__).parents[3]
PREFIX = [] if SETTING == "1" else shlex.split(SETTING)


def _linux(path: Path) -> str:
    """``path`` as the shell running the scripts sees it (WSL: /mnt/<drive>/...)."""
    text = str(path)
    if PREFIX and re.match(r"^[A-Za-z]:", text):
        return f"/mnt/{text[0].lower()}{text[2:]}".replace("\\", "/")
    return text


DEPLOY = _linux(REPO / "deploy" / "tenant")


def sh(script: str, check: bool = True) -> str:
    """Run ``script`` in ``deploy/tenant`` with ``lib.sh`` sourced; return stdout."""
    # Bytes, so Windows doesn't turn the script's newlines into CRLF.
    done = subprocess.run(
        [*PREFIX, "bash", "-s"],
        input=f"cd {shlex.quote(DEPLOY)}\nsource lib.sh\n{script}\n".encode(),
        capture_output=True,
        timeout=3000,
    )
    out = done.stdout.decode("utf-8", "replace")
    if check and done.returncode != 0:
        err = done.stderr.decode("utf-8", "replace")
        raise AssertionError(
            f"exit {done.returncode}\n{script}\n--- stdout\n{out[-3000:]}"
            f"\n--- stderr\n{err[-3000:]}"
        )
    return out


def run(tenant: str, command: str, check: bool = True) -> str:
    """Run ``command`` in a new orchestrator container of ``tenant``'s stack."""
    return sh(
        f'tenant_compose {tenant} run --rm -T -v "$HERE/smoke.py:/scripts/smoke.py:ro" '
        f"orchestrator {command} 2>/dev/null | grep -v '^Acquirium:' || true",
        check,
    )


@pytest.fixture(scope="module")
def tenants():
    """Two fresh tenants on a running cluster; tenant A has run the smoke study."""
    sh(
        'docker image inspect "$FLEXORCH_IMAGE" >/dev/null 2>&1 || docker build '
        '-f ../docker/Dockerfile -t "$FLEXORCH_IMAGE" ../.. >/dev/null'
    )
    sh("bash init_cluster.sh >/dev/null 2>&1")
    key = uuid.uuid4().hex[:6]
    a, b, restored = f"iso_a_{key}", f"iso_b_{key}", f"iso_r_{key}"
    try:
        for tenant in (a, b):
            sh(f"bash provision_tenant.sh {tenant} >/dev/null 2>&1")
        smoke = json.loads(run(a, "python /scripts/smoke.py").strip().splitlines()[-1])
        yield {"a": a, "b": b, "restored": restored, "smoke": smoke}
    finally:
        for tenant in (a, b, restored):
            sh(f"bash deprovision_tenant.sh {tenant} >/dev/null 2>&1", check=False)


def container(tenant: str) -> str:
    return sh(f"tenant_compose {tenant} ps -q acquirium").strip()


def inspect(name: str, template: str) -> str:
    return sh(f"docker inspect {name} --format {shlex.quote(template)}").strip()


PROBE = """
import socket, sys
try:
    socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=5).close()
    print("open")
except OSError as exc:
    print(f"closed {type(exc).__name__}")
"""


def reach(tenant: str, host: str, port: int) -> str:
    """Whether ``tenant``'s orchestrator can open a TCP connection to host:port."""
    return run(tenant, f"python -c {shlex.quote(PROBE)} {host} {port}").strip()


# The scripts run inside a stack, in mode 3


def test_a_study_runs_inside_a_tenant_stack(tenants):
    smoke = tenants["smoke"]
    assert smoke["counts"] == {"succeeded": 2}
    assert smoke["costs"]["today"] > smoke["costs"]["yesterday"]  # its pump ran more
    report = json.loads(run(tenants["a"], "flex-orch verify"))
    assert report["ok"] and report["studies"]["smoke"]["result"]["tags"] == [
        "today",
        "yesterday",
    ]


# Isolation (D14): database, network, volume and secret


LOGIN = """
import psycopg, sys
try:
    psycopg.connect(sys.argv[1], connect_timeout=5).close()
    print("connected")
except psycopg.OperationalError as exc:
    print("refused", str(exc).strip().splitlines()[-1])
"""


def login(tenant: str, dsn: str) -> str:
    """Log in from ``tenant``'s Acquirium, with its own pgpass file."""
    return sh(
        f"docker exec {container(tenant)} python -c {shlex.quote(LOGIN)} "
        f"{shlex.quote(dsn)}"
    ).strip()


def test_a_tenant_reaches_only_its_own_database(tenants):
    a, b = tenants["a"], tenants["b"]
    assert login(b, f"host=flexorch-pg dbname={b} user={b}") == "connected"
    # Its own login, another tenant's database: refused by pg_hba (sameuser).
    assert login(b, f"host=flexorch-pg dbname={a} user={b}").startswith("refused")
    # Another tenant's role: there's no password for it here.
    assert login(b, f"host=flexorch-pg dbname={a} user={a}").startswith("refused")
    assert login(b, f"host=flexorch-pg dbname=postgres user={b}").startswith("refused")
    facts = sh(f"""admin_sql -tA <<'SQL'
SELECT has_database_privilege('{b}', '{a}', 'CONNECT'),
       has_database_privilege('public', '{a}', 'CONNECT'),
       rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls
FROM pg_roles WHERE rolname = '{b}';
SQL""").strip()
    assert facts == "f|f|f|f|f|f|f"


def test_networks_dont_cross_tenants(tenants):
    a, b = tenants["a"], tenants["b"]
    assert reach(b, "acquirium", 8000) == "open"  # its own Acquirium
    a_acquirium = inspect(container(a), "{{.Name}}").lstrip("/")
    a_address = inspect(
        container(a),
        f'{{{{(index .NetworkSettings.Networks "flexorch_{a}").IPAddress}}}}',
    )
    for host in (a_acquirium, a_address):
        assert reach(b, host, 8000).startswith("closed")
    # The orchestrator isn't on any database network, and nothing goes out.
    assert reach(b, "flexorch-pg", 5432).startswith("closed")
    assert reach(b, "1.1.1.1", 443).startswith("closed")
    # Postgres is on each tenant's database network, but no tenant is on another's.
    networks = inspect(
        container(b),
        "{{range $name, $_ := .NetworkSettings.Networks}}{{$name}} {{end}}",
    )
    assert set(networks.split()) == {f"flexorch_{b}", f"flexorch_{b}_db"}


def test_volumes_and_secrets_belong_to_one_tenant(tenants):
    for tenant in (tenants["a"], tenants["b"]):
        mounts = inspect(
            container(tenant),
            "{{range .Mounts}}{{.Type}}={{if .Name}}{{.Name}}{{else}}{{.Source}}{{end}}"
            ">{{.Destination}} {{end}}",
        ).split()
        assert f"volume=flexorch_{tenant}_data>/data" in mounts
        secret = [m for m in mounts if m.endswith(">/run/secrets/pgpass")]
        assert secret and f"/tenants/{tenant}/pgpass>" in secret[0]
        assert not [m for m in mounts if "docker.sock" in m]
        assert len(mounts) == 2  # nothing else: no host paths, no other volume
    # The password is only in the secret file: not in any container's
    # environment or command line, and the orchestrator has no secrets at all.
    a = tenants["a"]
    password = sh(f'cut -d: -f5 "$FLEXORCH_SECRETS/tenants/{a}/pgpass"').strip()
    assert len(password) == 40
    config = inspect(container(a), "{{json .Config.Env}} {{json .Config.Cmd}}")
    assert password not in config
    assert "No such file" in run(a, "sh -c 'ls /run/secrets 2>&1 || true'")
    assert password not in run(a, "env")


def test_containers_are_locked_down(tenants):
    for tenant in (tenants["a"], tenants["b"]):
        found = inspect(
            container(tenant),
            "{{.Config.User}} {{.HostConfig.ReadonlyRootfs}} "
            "{{json .HostConfig.CapDrop}} {{json .HostConfig.SecurityOpt}}",
        )
        assert found == '1000:1000 true ["ALL"] ["no-new-privileges:true"]'
    assert run(tenants["a"], "id -u").strip() == "1000"
    assert "Read-only file system" in run(
        tenants["a"], "sh -c 'touch /usr/x 2>&1 || true'"
    )


# The restore drill (D14): delete a tenant, restore it at a chosen time


def test_the_restore_drill(tenants):
    a, restored = tenants["a"], tenants["restored"]
    before = run(a, "flex-orch verify --samples 50")
    assert json.loads(before)["ok"]
    sh(f"bash backup_tenant.sh {a} >/dev/null 2>&1")
    target = sh("sleep 3; date -u '+%Y-%m-%d %H:%M:%S'; sleep 3").strip()
    later = json.loads(
        run(a, "python /scripts/smoke.py later").strip().splitlines()[-1]
    )
    assert later["counts"] == {"succeeded": 2}  # after the chosen time
    sh(f"bash deprovision_tenant.sh {a} >/dev/null 2>&1")
    assert (
        sh(f"admin_sql -tA <<<\"SELECT 1 FROM pg_database WHERE datname = '{a}'\"")
        == ""
    )
    sh(f"bash restore_tenant.sh {a} '{target}' --into {restored} >/dev/null")
    report = sh(
        f"printf %s {shlex.quote(before)} > /tmp/flexorch-before-{restored}.json\n"
        f"tenant_compose {restored} run --rm -T "
        f"-v /tmp/flexorch-before-{restored}.json:/tmp/before.json:ro orchestrator "
        "flex-orch verify --samples 50 --against /tmp/before.json 2>/dev/null "
        "| grep -v '^Acquirium:' || true"
    )
    after = json.loads(report)
    assert after["differences"] == [] and after["problems"] == []
    assert after["ok"] and "later" not in after["studies"]
