# Configuration

What an application in front of flex-orchestrator (a web app, a desktop app,
a control plane) sets, and what it supplies. flex-orchestrator makes no
policy about identities, certificates or secrets: the app does. It never
stores passwords, never issues certificates, and checks tokens rather than
issuing them.

`deploy/` is a **reference deployment**, for development, tests and the
examples. Its scripts show one way to run tenant stacks, and they take every
secret the app supplies; the few they generate when none is given are
conveniences, not policy. An app is free to run the image and the
compose files its own way, with this page as the contract.

## Who does what

| | The app | flex-orchestrator |
| --- | --- | --- |
| Users, passwords, logins | Holds them (its own accounts, or an identity provider) | Never sees them |
| API tokens | Issues JWTs (an identity provider), or makes static tokens | Checks them: JWTs against the issuer's keys, static tokens by their SHA-256 |
| Roles | Puts `tenant` and `role` claims in the JWT, or registers a token with a role | Enforces viewer < editor < owner per route |
| TLS certificates | Obtains and renews them, or terminates TLS itself | Serves what it's given; never makes one |
| Database and backup secrets | May supply them | Generates the ones not supplied (reference deployment) |
| Routes and the UI | Defines them | Serves the API under a path prefix it's told |

## The API server

`flex-orch serve` (`create_app(Settings.from_env())`), one per tenant. All
settings are environment variables; none is secret except the static token
hashes, which aren't the tokens.

| Variable | | |
| --- | --- | --- |
| `FLEXORCH_TENANT` | required | The tenant it serves. A JWT's `tenant` claim must name it. |
| `FLEXORCH_OIDC_ISSUER` | for JWTs | The identity provider; its keys are discovered from it. |
| `FLEXORCH_OIDC_AUDIENCE` | with an issuer | The audience JWTs must carry. |
| `FLEXORCH_OIDC_JWKS_URL` | optional | The issuer's keys, when discovery isn't right. |
| `FLEXORCH_API_TOKENS` | for static tokens | `user:role:sha256:<hex>` entries, `;` or newline separated. |
| `FLEXORCH_API_TOKENS_FILE` | for static tokens | The same, a file, one per line. |
| `FLEXORCH_ACQUIRIUM_URL` | required in a stack | The tenant's Acquirium. |
| `FLEXORCH_PLUGINS` | optional | The installed plugins to load, by name, comma-separated (an allowlist). |
| `FLEXORCH_CORS_ORIGINS` | optional | Browser origins allowed to call it, comma-separated; none by default. Not needed when the UI is served from the same origin. |
| `FLEXORCH_ROOT_PATH` | behind a gateway | The prefix it's served under, e.g. `/tenant_a`. The gateway strips it; the server puts it back in its schema and docs. |
| `FLEXORCH_IDLE_MINUTES` | optional | Stop after this many minutes with no request and no job. Unset: never. |

At least one way to authenticate (an issuer, or static tokens) is required.
Both can be set at once.

**JWTs** are checked for signature (the issuer's JWKS), `iss`, `aud`, `exp`,
a `tenant` claim equal to `FLEXORCH_TENANT`, and a `role` claim of `viewer`,
`editor` or `owner`. The user is the `sub` claim (or `preferred_username`).

**Static tokens** are opaque random strings the app makes. Only their SHA-256
is configured (`flex_orchestrator.server.settings.hash_token`, or
`sha256sum`), so the token itself never reaches the server's host.

**Local mode** (`flex-orch serve --local`, `Settings.local()`) is for a desktop
app on one machine: no tokens, every request is the local user as owner, and
only loopback clients, `Host` and `Origin` are accepted. It needs no
certificate.

Endpoints the app uses besides the API: `GET /health` (no token; the tenant's
name), `GET /ready`, `GET /jobs/{id}/events` (progress, Server-Sent Events),
and the schema, `docs/schemas/openapi.json` (`flex-orch openapi`), to generate
its client from.

## The client

`Orchestrator(url=..., token=..., cafile=...)`:

- `token`, else `FLEXORCH_TOKEN`, else the token `flex-orch login` saved.
- `cafile`: a CA to trust besides the system's, for a certificate the app
  issued itself (an internal CA on an air-gapped site).

## The reference deployment

Every file below is under `$FLEXORCH_SECRETS` (default `~/.flexorch/secrets`),
readable by uid 1000 only. **Supplied** means the app may put it there (or pass
it to the script named) and it's used as is; **generated** means the script
makes one only when none was supplied.

### The cluster (`init_cluster.sh`)

| File in `cluster/` | | |
| --- | --- | --- |
| `postgres_password` | supplied or generated | The Postgres admin's password. |
| `backup_pass` | supplied or generated | The passphrase backups are encrypted with. Without it no backup can be read: keep a copy off the host. |
| `s3.env` | supplied or generated | `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_DEFAULT_REGION` for the backup store. |
| `ca.crt`, `s3.crt`, `s3.key` | supplied or generated | The backup store's TLS. Internal: only pgBackRest and the backup scripts use it, on the cluster's internal network. |
| `pgbackrest.conf` | supplied or written | pgBackRest's configuration, written from the files above when missing. Supply it to back up to a real S3 (and drop the SeaweedFS service). |

Supplied secrets that end up in a configuration file (the S3 keys, the backup
passphrase) must be at least 16 letters, digits or `._~+/=-`.

### Tenants (`provision_tenant.sh`, `api_token.sh`)

| | | |
| --- | --- | --- |
| Database password | supplied or generated | `provision_tenant.sh <t> --password-file FILE` (or `-` for stdin): at least 16 letters, digits or `._~+/=-`. Kept only in `tenants/<t>/pgpass`, mounted only into the tenant's Acquirium. Given again, it changes the password. |
| API tokens | supplied or generated | `api_token.sh <t> <user> <role> --sha256 <hex>` registers a token the app made, by its hash. Without `--sha256`, it makes one and prints it once. Hashes are kept in `tenants/<t>/api_tokens`. |
| Identity provider | setting | `provision_tenant.sh <t> --oidc-issuer URL --oidc-audience AUD [--oidc-jwks-url URL]`. Kept for every start; without it, the environment's `FLEXORCH_OIDC_*` when the stack starts. |
| CORS, idle, backups | settings | `--cors-origins LIST`, `--idle-minutes N`, `--tenant-backups on\|off`. |

Settings (not secret) are in `settings/tenant_<t>.env`.

### The gateway (`deploy/gateway/init_gateway.sh`)

It never makes a certificate.

| | |
| --- | --- |
| `--cert FILE --key FILE` | HTTPS with the app's certificate (and chain) and key. Installed as `gateway/tls.crt` and `tls.key`. |
| `--https` | HTTPS with the certificate already in `gateway/` (the app wrote it there). Run it again after a renewal. |
| `--plain-http` | Plain HTTP, published on 127.0.0.1 only: for a gateway this machine alone uses, or one behind the app's own TLS proxy (`FLEXORCH_GATEWAY_BIND` to publish it elsewhere; tokens then cross it in the clear). |

`FLEXORCH_GATEWAY_HTTPS_PORT`, `FLEXORCH_GATEWAY_HTTP_PORT` and
`FLEXORCH_GATEWAY_BIND` set where it listens. The app needn't use this gateway
at all: any reverse proxy that routes `/<tenant>/` to the tenant's API with the
prefix stripped (and sets `FLEXORCH_ROOT_PATH`) does the same.
