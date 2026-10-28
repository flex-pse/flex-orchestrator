"""Set up tenants in Docker from Python, then use them through the gateway.

Where ``local_mode.py`` and ``users_and_tokens.py`` run a server in a thread,
this one runs the real deployment: the shared cluster (Postgres), the gateway
(Traefik), and two tenant stacks, each with its own Acquirium and API in
containers. The script sets them up with the deploy scripts, the way the app
in front of flex-orchestrator would: the app makes the secrets (here, each
tenant's database password and each user's token) and hands them over, and
the deploy scripts keep only what they need (the password file, the tokens'
hashes). Then it uses them as three users would:

- alice, the owner of ``demo_a``, runs the RO surrogate sweep there;
- vic, a viewer of ``demo_a``, reads it, and can't delete it;
- carol, the owner of ``demo_b``, can't get into ``demo_a``, and sees only
  ``demo_b``'s studies.

    python examples/server_examples/docker_tenants.py [--keep]

The gateway serves plain HTTP on 127.0.0.1 only (``init_gateway.sh
--plain-http``): this machine is the only client. A deployment others use gets
the app's certificate instead (``init_gateway.sh --cert FILE --key FILE``).

The tenants are removed at the end, unless ``--keep``; the cluster and the
gateway are left running (``docker compose -f deploy/gateway/compose.gateway.yaml
down`` stops the gateway). The first run builds the image, which takes a while.

It needs Docker with Compose v2 where ``bash`` runs: here on Linux or macOS;
on Windows, in WSL (``FLEXORCH_DOCKER_SHELL``, ``wsl.exe --`` by default, runs
the commands there). The gateway listens on ``FLEXORCH_DEMO_HTTP_PORT``
(18080); its HTTPS port, ``FLEXORCH_DEMO_HTTPS_PORT`` (18443), serves nothing.
"""

import hashlib
import os
import re
import secrets
import shlex
import subprocess
import sys

from serving import HERE, STUDY, new_study, report

from flex_orchestrator import Orchestrator
from flex_orchestrator.errors import AccessError

REPO = HERE.parents[1]
HTTPS = os.environ.get("FLEXORCH_DEMO_HTTPS_PORT", "18443")
HTTP = os.environ.get("FLEXORCH_DEMO_HTTP_PORT", "18080")
GATEWAY = f"http://localhost:{HTTP}"
SHELL = shlex.split(
    os.environ.get("FLEXORCH_DOCKER_SHELL", "wsl.exe --" if os.name == "nt" else "")
)


def where_bash_runs(path) -> str:
    """``path`` as bash sees it: in WSL, C:\\x is /mnt/c/x."""
    text = str(path)
    if SHELL and re.match(r"^[A-Za-z]:", text):
        return f"/mnt/{text[0].lower()}{text[2:]}".replace("\\", "/")
    return text


def deploy(script: str) -> str:
    """Run ``script`` with bash in deploy/tenant, as an operator would.

    The braces make bash read all of it first, so a command that reads stdin
    can't swallow the rest. Returns its stdout.
    """
    folder = shlex.quote(where_bash_runs(REPO / "deploy" / "tenant"))
    ports = f"FLEXORCH_GATEWAY_HTTPS_PORT={HTTPS} FLEXORCH_GATEWAY_HTTP_PORT={HTTP}"
    wrapped = f"{{\ncd {folder}\nexport {ports}\n{script}\n}}\n"
    done = subprocess.run(
        [*SHELL, "bash", "-s"], input=wrapped.encode(), capture_output=True
    )
    if done.returncode != 0:
        sys.exit(f"{script}\nfailed:\n{done.stderr.decode(errors='replace')[-2000:]}")
    return done.stdout.decode()


def new_tenant(tenant: str) -> None:
    """Provision ``tenant`` with a database password the app made.

    It goes to the script on stdin (``--password-file -``), inside the script
    bash reads, so it's on no command line.
    """
    password = secrets.token_urlsafe(30)
    deploy(f"echo {password} | bash provision_tenant.sh {tenant} "
           "--password-file - 2>/dev/null")  # fmt: skip


def new_token(tenant: str, user: str, role: str) -> str:
    """A token the app made for ``user``; the tenant keeps only its hash."""
    token = secrets.token_urlsafe(32)
    digest = hashlib.sha256(token.encode()).hexdigest()
    deploy(f"bash api_token.sh {tenant} {user} {role} --sha256 {digest} 2>/dev/null")
    return token


def refused(call) -> str:
    try:
        call()
    except AccessError as exc:
        return f"refused ({exc.status})"
    return "allowed"


if __name__ == "__main__":
    print("1. the image, the cluster and the gateway")
    deploy(
        'docker image inspect "${FLEXORCH_IMAGE:-flexorch:dev}" >/dev/null 2>&1 || '
        'docker build -q -f ../docker/Dockerfile -t "${FLEXORCH_IMAGE:-flexorch:dev}" '
        "../.. >/dev/null"
    )
    deploy("bash init_cluster.sh 2>/dev/null")
    deploy("bash ../gateway/init_gateway.sh --plain-http 2>/dev/null")

    print("2. two tenants, three users")
    for tenant in ("demo_a", "demo_b"):
        new_tenant(tenant)
    alice_token = new_token("demo_a", "alice", "owner")
    vic_token = new_token("demo_a", "vic", "viewer")
    carol_token = new_token("demo_b", "carol", "owner")
    for tenant in ("demo_a", "demo_b"):
        deploy(f"bash start_tenant.sh {tenant} 2>/dev/null")
    print(f"   demo_a's API: {GATEWAY}/demo_a/docs")

    try:
        print("3. alice runs the surrogate sweep in demo_a")
        alice = Orchestrator(url=f"{GATEWAY}/demo_a", token=alice_token)
        new_study("ro_in_docker", alice)  # data and spec, its tariff file too
        alice.execute_study(STUDY)  # on demo_a's job queue, in its container
        report(alice)

        print("4. vic reads it; carol can't")
        vic = Orchestrator(
            url=f"{GATEWAY}/demo_a", token=vic_token, study="ro_in_docker"
        )
        print("   vic sees:", [r.tag for r in vic.stored_model_results()])
        print("   vic deletes a result:",
              refused(lambda: vic.delete("result", "linear_surrogate")))  # fmt: skip
        carol_at_a = Orchestrator(url=f"{GATEWAY}/demo_a", token=carol_token)
        print("   carol in demo_a:", refused(carol_at_a.studies))
        carol = Orchestrator(url=f"{GATEWAY}/demo_b", token=carol_token)
        print("   carol's studies in demo_b:", carol.studies())
    finally:
        if "--keep" not in sys.argv:
            print("5. removing the tenants")
            for tenant in ("demo_a", "demo_b"):
                deploy(f"bash deprovision_tenant.sh {tenant} 2>/dev/null")
