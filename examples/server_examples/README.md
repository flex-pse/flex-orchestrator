# Server examples

The HTTP API (`pip install -e ".[server]"`) and its client,
`Orchestrator(url=...)`, which has the local orchestrator's methods. Each
script starts a server in a thread on a fresh store, `tmp_flexorch` here, runs
the [`ro_study`](../ro_study/README.md) surrogate sweep through it, and stops
it. The server logs every request, so you can see which route each call is.
Its routes are at `<url>/docs` while it runs.

| Script | Shows |
| --- | --- |
| [`local_mode.py`](local_mode.py) | Local mode (`flex-orch serve --local`), for a local app: no tokens, every request is you, as owner, from this machine only. The study, its data and spec (with its tariff file) stored; the study run as a job and followed; the results read back; the commands; the study exported. |
| [`users_and_tokens.py`](users_and_tokens.py) | A server with users: a dummy token each for alice (editor) and vic (viewer), of which the server keeps only the hashes. alice stores and runs the study; vic reads it, and sees who ran it; the server refuses vic's delete (403), a request with no token (401) and a wrong token (401). |
| [`serving.py`](serving.py) | What both share: `start_server(settings)`, and the study from `ro_study`. |

```
python examples/server_examples/local_mode.py
python examples/server_examples/users_and_tokens.py
```

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
