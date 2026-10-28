"""Set up tenants in Docker from Python, then use them through the gateway.

Where ``local_mode.py`` and ``users_and_tokens.py`` run a server in a thread,
this one runs the real deployment: the shared cluster (Postgres), the gateway
(Traefik, HTTPS), and two tenant stacks, each with its own Acquirium and API
in containers. The script sets them up with the deploy scripts, the way an
operator would, then uses them as three users would:

- alice, the owner of ``demo_a``, runs the RO surrogate sweep there;
- vic, a viewer of ``demo_a``, reads it, and can't delete it;
- carol, the owner of ``demo_b``, can't get into ``demo_a``, and sees only
  ``demo_b``'s studies.

    python examples/server_examples/docker_tenants.py [--keep]

The tenants are removed at the end, unless ``--keep``; the cluster and the
gateway are left running (``docker compose -f deploy/gateway/compose.gateway.yaml
down`` stops the gateway). The first run builds the image, which takes a while.

It needs Docker with Compose v2 where ``bash`` runs: here on Linux or macOS;
on Windows, in WSL (``FLEXORCH_DOCKER_SHELL``, ``wsl.exe --`` by default, runs
the commands there). The gateway listens on ``FLEXORCH_DEMO_HTTPS_PORT``
(18443) and ``FLEXORCH_DEMO_HTTP_PORT`` (18080).
"""

import os
import re
import shlex
import subprocess
import sys

from serving import HERE, STUDY, new_study, report

from flex_orchestrator import Orchestrator
from flex_orchestrator.errors import AccessError

REPO = HERE.parents[1]
HTTPS = os.environ.get("FLEXORCH_DEMO_HTTPS_PORT", "18443")
HTTP = os.environ.get("FLEXORCH_DEMO_HTTP_PORT", "18080")
GATEWAY = f"https://localhost:{HTTPS}"
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
    deploy("bash ../gateway/init_gateway.sh 2>/dev/null")
    ca = HERE / "tmp_flexorch" / "gateway-ca.crt"  # the gateway's own CA
    ca.parent.mkdir(exist_ok=True)
    ca.write_text(deploy('source lib.sh; cat "$GATEWAY_DIR/ca.crt"'))

    print("2. two tenants, three users")
    for tenant in ("demo_a", "demo_b"):
        deploy(f"bash provision_tenant.sh {tenant} 2>/dev/null")
    alice_token = deploy("bash api_token.sh demo_a alice owner 2>/dev/null").strip()
    vic_token = deploy("bash api_token.sh demo_a vic viewer 2>/dev/null").strip()
    carol_token = deploy("bash api_token.sh demo_b carol owner 2>/dev/null").strip()
    for tenant in ("demo_a", "demo_b"):
        deploy(f"bash start_tenant.sh {tenant} 2>/dev/null")
    print(f"   demo_a's API: {GATEWAY}/demo_a/docs")

    try:
        print("3. alice runs the surrogate sweep in demo_a")
        alice = Orchestrator(url=f"{GATEWAY}/demo_a", token=alice_token, cafile=ca)
        new_study("ro_in_docker", alice)  # data and spec, its tariff file too
        alice.execute_study(STUDY)  # on demo_a's job queue, in its container
        report(alice)

        print("4. vic reads it; carol can't")
        vic = Orchestrator(
            url=f"{GATEWAY}/demo_a", token=vic_token, cafile=ca, study="ro_in_docker"
        )
        print("   vic sees:", [r.tag for r in vic.stored_model_results()])
        print("   vic deletes a result:",
              refused(lambda: vic.delete("result", "linear_surrogate")))  # fmt: skip
        carol_at_a = Orchestrator(url=f"{GATEWAY}/demo_a", token=carol_token, cafile=ca)
        print("   carol in demo_a:", refused(carol_at_a.studies))
        carol = Orchestrator(url=f"{GATEWAY}/demo_b", token=carol_token, cafile=ca)
        print("   carol's studies in demo_b:", carol.studies())
    finally:
        if "--keep" not in sys.argv:
            print("5. removing the tenants")
            for tenant in ("demo_a", "demo_b"):
                deploy(f"bash deprovision_tenant.sh {tenant} 2>/dev/null")
