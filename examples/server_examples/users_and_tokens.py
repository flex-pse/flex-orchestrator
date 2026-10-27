"""The HTTP server with users and tokens: who may do what.

A server that isn't in local mode checks a bearer token on every request.
Here it has static tokens for two users, as ``deploy/tenant/api_token.sh``
makes them for a deployed tenant: a random token for each, of which the server
keeps only the hash, ``user:role:sha256:<hex>``. alice is an editor, so she
stores and runs the study; vic is a viewer, so he reads it but can't delete
it. (With an identity provider instead, the tokens are its JWTs, and the
server is given ``FLEXORCH_OIDC_ISSUER``; the client is the same.)

    python examples/server_examples/users_and_tokens.py

From a shell, the server is ``FLEXORCH_TENANT=lab FLEXORCH_API_TOKENS=...
flex-orch --data-dir ... serve``, and a user saves their token once with
``flex-orch login``.
"""

import secrets
import urllib.error
import urllib.request

from serving import STUDY, new_study, report, start_server

from flex_orchestrator import Orchestrator
from flex_orchestrator.errors import AccessError
from flex_orchestrator.server import Settings, hash_token
from flex_orchestrator.server.settings import parse_api_tokens

# Dummy tokens, made fresh for this run. Each is handed to its user once; the
# server is given only their hashes.
ALICE = secrets.token_urlsafe(32)
VIC = secrets.token_urlsafe(32)
API_TOKENS = f"alice:editor:{hash_token(ALICE)}\nvic:viewer:{hash_token(VIC)}"


def refused(call) -> str:
    """Run ``call``; return how the server refused it."""
    try:
        call()
    except AccessError as exc:
        return f"refused ({exc.status})"
    return "allowed"


if __name__ == "__main__":
    print("the server's tokens file:\n" + API_TOKENS)
    settings = Settings(tenant="lab", api_tokens=parse_api_tokens(API_TOKENS, "lab"))
    server, thread, url = start_server(settings)
    try:
        # alice, an editor: stores the data and spec and runs the study
        alice = Orchestrator(url=url, token=ALICE)
        new_study("ro_with_tokens", alice)
        alice.execute_study(STUDY)

        # vic, a viewer: reads what alice stored, under her name
        vic = Orchestrator(url=url, token=VIC, study="ro_with_tokens")
        report(vic)
        user = vic.result("linear_surrogate").metadata["flexorch_user"]
        print("linear_surrogate was run by", user)

        # what the server refuses
        delete = lambda: vic.delete("result", "linear_surrogate")  # noqa: E731
        print("vic deletes a result:", refused(delete))
        try:  # a plain request, with no Authorization header
            urllib.request.urlopen(f"{url}/studies")
        except urllib.error.HTTPError as exc:
            print(f"no token: refused ({exc.code})")
        wrong = Orchestrator(url=url, token=secrets.token_urlsafe(32))
        print("a wrong token:", refused(wrong.studies))
    finally:
        server.should_exit = True
        thread.join()
