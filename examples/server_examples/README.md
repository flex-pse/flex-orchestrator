# Server examples

The HTTP API (`pip install -e ".[server]"`) and its client,
`Orchestrator(url=...)`, which has the local orchestrator's methods. The first
two scripts start a server in a thread on a fresh store, `tmp_flexorch` here;
the third sets up the real deployment in Docker. Each runs the
[`ro_study`](../ro_study/README.md) surrogate sweep through it. The server
logs every request, so you can see which route each call is. Its routes are
at `<url>/docs` while it runs.

| Script | Shows |
| --- | --- |
| [`local_mode.py`](local_mode.py) | Local mode (`flex-orch serve --local`), for a local app: no tokens, every request is you, as owner, from this machine only. The study, its data and spec (with its tariff file) stored; the study run as a job and followed; the results read back; the commands; the study exported. |
| [`users_and_tokens.py`](users_and_tokens.py) | A server with users: a dummy token each for alice (editor) and vic (viewer), of which the server keeps only the hashes. alice stores and runs the study; vic reads it, and sees who ran it; the server refuses vic's delete (403), a request with no token (401) and a wrong token (401). |
| [`docker_tenants.py`](docker_tenants.py) | The deployment, set up from Python with the deploy scripts, as the app in front would: the cluster, the gateway, two tenant stacks in containers with database passwords the script makes, and three users' tokens it makes and registers by hash. alice (owner) runs the sweep in `demo_a`; vic (viewer) reads it and can't delete it (403); carol, `demo_b`'s owner, can't get into `demo_a` (401). The tenants are removed after, unless `--keep`. Needs Docker (on Windows, in WSL). |
| [`serving.py`](serving.py) | What they share: `start_server(settings)` (the first two), and the study from `ro_study`. |

```
python examples/server_examples/local_mode.py
python examples/server_examples/users_and_tokens.py
python examples/server_examples/docker_tenants.py      # Docker; a while the first time
```

`docker_tenants.py` runs its commands with bash where Docker is: here on Linux
or macOS, and in WSL on Windows (`FLEXORCH_DOCKER_SHELL`, `wsl.exe --` by
default, e.g. `wsl.exe -d Ubuntu --`). Its gateway is plain HTTP on
127.0.0.1, port 18080 (`FLEXORCH_DEMO_HTTP_PORT`), since this machine is its
only client; a deployment others use gets the app's certificate
(`init_gateway.sh --cert FILE --key FILE`). What the app supplies is in
[docs/configuration.md](../../docs/configuration.md). It's tested in
`src/flex_orchestrator/tests/test_gateway.py`, opt-in like the other Docker
tests (`FLEXORCH_TEST_DEPLOY`).

## The tokens

`users_and_tokens.py` makes its tokens itself. In a deployment they come from
one of two places, and the client is the same either way
(`Orchestrator(url=..., token=...)`, or `FLEXORCH_TOKEN`, or a token saved
with `flex-orch login`):

- **Static tokens**: `deploy/tenant/api_token.sh <tenant> <user> <role>`
  prints a token once and adds `user:role:sha256:<hex>` to the tenant's
  `api_tokens` file (`FLEXORCH_API_TOKENS_FILE`). Delete the line to revoke it.
- **An identity provider** (Keycloak, or the web app's own): it holds the
  users and their passwords and issues JWTs with `tenant` and `role` claims;
  the server checks them against its keys (`FLEXORCH_OIDC_ISSUER`,
  `FLEXORCH_OIDC_AUDIENCE`). The orchestrator never stores passwords.
