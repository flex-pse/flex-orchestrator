# Tenant stacks

One stack per tenant (a group or project) on a shared Postgres cluster
(PLAN.md D14). A tenant gets its own database and role, password, volume,
networks and containers; nothing is shared between tenants but the cluster,
which each tenant reaches only as its own role.

This is the **reference deployment**: for development, tests and the
examples, and a starting point. The app in front of flex-orchestrator governs
identities, certificates and secrets; every script here takes the secrets it
supplies, and generates only the ones it doesn't. What the app sets and
supplies is in [docs/configuration.md](../../docs/configuration.md).

```
 gateway (Traefik, HTTPS) ──flexorch_<tenant>_edge──► api ─┐
              ┌──────────── flexorch_<tenant> (internal) ──┼─────────┐
              │ orchestrator (run on demand) ──► acquirium :8000 ◄───┘
              └───────────────────────────────────────┬──────────────┘
                                   flexorch_<tenant>_db │ (internal)
   flexorch_cluster (internal):  postgres (TimescaleDB, pgBackRest) ──► seaweedfs (S3, TLS)
```

The gateway is in [`deploy/gateway`](../gateway/README.md).

## What's here

| File | What it does |
| --- | --- |
| `compose.cluster.yaml` | Postgres 17 + TimescaleDB with WAL archiving, and SeaweedFS as the S3 store backups go to |
| `compose.tenant.yaml` | one tenant's stack: its Acquirium, its API, and the orchestrator container scripts run in |
| `pg_hba.conf` | a role may log in only to the database of its own name; the admin only over the socket |
| `init_cluster.sh` | the cluster's secrets (supplied, or generated when missing), bucket, pgBackRest stanza, first full backup; `FLEXORCH_BACKUPS=off` for none |
| `provision_tenant.sh <t> [options]` | role, database, password (supplied or generated), networks, gateway route, settings; then starts its Acquirium |
| `start_tenant.sh <t>` / `stop_tenant.sh <t>` | start (and wait for) or stop a tenant's Acquirium and API; its data is kept |
| `idle_tenants.sh` | from cron: stops the Acquirium of each tenant whose API timed out |
| `deprovision_tenant.sh <t>` | a final backup, then removes the stack, volumes, networks, database, role and secret |
| `backup_tenant.sh <t>` | a consistent dump and volume copy (and a graph export), encrypted to S3 |
| `backup_cluster.sh [full\|diff\|incr]` | the nightly pgBackRest backup, then every tenant's |
| `restore_tenant.sh <t> <time\|id> --into <new>` | point-in-time (or from a tenant backup) restore into a new tenant |
| `smoke.py` | a short study a tenant's orchestrator container runs, for checking a stack |
| `api_token.sh <t> <user> <role> [--sha256 HEX]` | registers a token the app made, by its hash; or makes one and prints it once. Only hashes are kept |

The image is `deploy/docker/Dockerfile`: one image for the orchestrator, the
Acquirium server and Ray nodes.

## Start

Run on a Linux host (or WSL) with Docker and Compose v2, as uid 1000 or with
`sudo` for `chown`:

```bash
docker build -f deploy/docker/Dockerfile -t flexorch:dev .
cd deploy/tenant
./init_cluster.sh
./provision_tenant.sh tenant_a
```

Settings, all optional:

- `FLEXORCH_SECRETS`: where secrets are kept, `~/.flexorch/secrets` by default.
  It must be on a filesystem with real permissions (not `/mnt/c` on WSL):
  libpq ignores a password file that others can read.
- `FLEXORCH_IMAGE`: the image, `flexorch:dev` by default.
- `FLEXORCH_BACKUPS`: `on` (the default) or `off`, recorded by
  `init_cluster.sh` (see [Backups off](#backups-off)).
- `FLEXORCH_BACKUP_BUCKET`, `PG_IMAGE`, `S3_IMAGE`, `AWS_CLI_IMAGE`,
  `DOCKER_COMPOSE`.

Settings that aren't secret (the cluster's backup switch, each tenant's
options, the gateway's routes) are in `$FLEXORCH_SECRETS/settings`.

## Operating it

**Add a tenant**, reachable at `https://<host>/<tenant>/`:

```bash
./provision_tenant.sh tenant_a                     # persistent
./provision_tenant.sh tenant_b --idle-minutes 30   # stops after 30 idle minutes
./start_tenant.sh tenant_a                         # its Acquirium and API
```

`provision_tenant.sh` options (run it again to change them):
- `--idle-minutes N`: 0 (the default) keeps the tenant running until it's
  stopped; N stops its API after N minutes with no requests and no job, and
  `idle_tenants.sh` then stops its Acquirium (and its drivers). The control
  plane runs `start_tenant.sh` when one of its users logs in.
- `--tenant-backups off`: the tenant isn't backed up.
- `--cors-origins https://ui.example.org`: the browser origins its API serves,
  for a UI on another origin than the gateway.
- `--password-file FILE` (or `-`, stdin): the database password the app made,
  instead of a random one; given again, it changes it.
- `--oidc-issuer URL --oidc-audience AUD` (and `--oidc-jwks-url URL`): the
  identity provider whose JWTs its API accepts.

**Add a user**: with an identity provider, users and passwords are its own:
give the user the `tenant` and `role` claims there. With static tokens, the
app makes the token and registers its hash,
`./api_token.sh tenant_a alice editor --sha256 <hex>` (viewer, editor or
owner); or `./api_token.sh tenant_a alice editor` makes one and prints it
once, and alice runs `flex-orch login` with it. To revoke one,
delete its line from `$FLEXORCH_SECRETS/tenants/tenant_a/api_tokens` and
restart the API (`./stop_tenant.sh tenant_a && ./start_tenant.sh tenant_a`).

**Back up**: schedule `./backup_cluster.sh` nightly (cron or a systemd timer);
`./backup_tenant.sh tenant_a` backs one tenant up now. Restore with
`./restore_tenant.sh` ([Backups and restore](#backups-and-restore)).

**Remove a tenant**: `./deprovision_tenant.sh tenant_a` (a final backup first,
if it's backed up).

## Run a study in a tenant's stack

The orchestrator container starts per job, on the tenant's network, with
`FLEXORCH_ACQUIRIUM_URL` set, so `Orchestrator()` and `flex-orch` use the
tenant's Acquirium (mode 3) with no arguments:

```bash
TENANT=tenant_a docker compose -f compose.tenant.yaml -p flexorch-tenant_a run --rm \
    -v "$PWD/scripts:/scripts:ro" orchestrator python /scripts/my_study.py
TENANT=tenant_a docker compose -f compose.tenant.yaml -p flexorch-tenant_a run --rm \
    orchestrator flex-orch verify
```

Its `/work` is the tenant's scratch volume; everything else is read-only.

## The HTTP API

A tenant's API (`flex-orch serve`, PLAN.md M7) is the `api` service, which
`start_tenant.sh` starts:

```bash
./api_token.sh tenant_a alice editor --sha256 <hex>   # a token the app made
./start_tenant.sh tenant_a
```

It publishes no port. The gateway reaches it on the tenant's edge network and
serves it at `https://<host>/tenant_a/` (`Orchestrator(url=
"https://<host>/tenant_a", token=...)`; its docs at `/tenant_a/docs`).
Inside the stack, it's `http://api:8080`. Tokens are viewer, editor or owner.
Only each token's SHA-256 is kept, in `$FLEXORCH_SECRETS/tenants/<t>/api_tokens`;
delete a line and restart `api` to revoke one. With an identity provider
instead, `provision_tenant.sh <t> --oidc-issuer URL --oidc-audience AUD`: JWTs
must carry the tenant's name in a `tenant` claim and a `role` claim.

## Secrets

Every secret is a file under `$FLEXORCH_SECRETS` (mode 600, uid 1000). The app
may supply each one, and the scripts use it; they generate only the ones it
doesn't ([docs/configuration.md](../../docs/configuration.md) lists them):

- `init_cluster.sh`, in `$FLEXORCH_SECRETS/cluster`: the Postgres admin
  password, the backup passphrase, the S3 keys, and the backup store's internal
  TLS certificate. A file already there is used as is.
- `provision_tenant.sh --password-file`: the tenant's database password,
  written only to `$FLEXORCH_SECRETS/tenants/<t>/pgpass`, which is mounted only
  into that tenant's Acquirium as a libpq password file. Given again, it changes
  the password. The password is never in an environment variable, a command
  line, a DSN or a log; users and the orchestrator never see it.
- `api_token.sh --sha256`: a token's hash; the token itself stays with the app.

## Backups and restore

- **Point-in-time:** pgBackRest archives every WAL segment (at least once a
  minute) to S3, encrypted, and `backup_cluster.sh` takes the nightly backup
  (four full backups are kept). Any tenant can be restored to any time since
  the oldest one.
- **Per tenant:** `backup_tenant.sh` stops the tenant's Acquirium for a moment,
  dumps its database and copies its volume at the same point, exports its graph,
  and uploads all three, encrypted, to `tenants/<t>/<id>/`. `backup_cluster.sh`
  runs it for every tenant.
- **Restore:** `restore_tenant.sh <t> "2026-10-26 23:30:00" --into <new>` brings
  the database back to that time (in a throwaway Postgres, from pgBackRest),
  takes the volume from the latest tenant backup at or before it, provisions
  `<new>`, loads both, starts it and resyncs Acquirium's metadata graph. With a
  backup id instead of a time, the database comes from that backup's dump.
  Then check it: `flex-orch verify --against <a report from before>`.

Schedule `backup_cluster.sh` nightly (cron or a systemd timer), and rehearse a
restore every quarter (PLAN.md §9). The restore drill in
`src/flex_orchestrator/tests/test_deploy.py` is that rehearsal.

### Backups off

For a cluster that needs none (a test host, or data kept elsewhere):

```bash
FLEXORCH_BACKUPS=off ./init_cluster.sh
```

It's recorded in `$FLEXORCH_SECRETS/settings/cluster.env`, and every script
follows it: there's no SeaweedFS and no WAL archiving, `backup_cluster.sh` and
`backup_tenant.sh` do nothing and say so, `restore_tenant.sh` refuses, and
`deprovision_tenant.sh` takes no final backup. `FLEXORCH_BACKUPS=on
./init_cluster.sh` turns them on again (Postgres restarts with archiving, and
takes its first full backup); there's nothing to restore from before that.
One tenant opts out with `provision_tenant.sh <t> --tenant-backups off`.

## Tests

```bash
FLEXORCH_TEST_DEPLOY=1 pytest src/flex_orchestrator/tests/test_deploy.py
```

On Windows, with Docker in WSL: `FLEXORCH_TEST_DEPLOY="wsl.exe -d Ubuntu --"`.
They provision two fresh tenants on the cluster and check isolation (database,
network, volume, secret, hardening), run the smoke study in mode 3, and run the
restore drill; the tenants are removed afterwards.

`test_gateway.py` (the same setting) restarts the gateway on ports 18443 and
18080, with a certificate it makes as the app would supply one, and puts four
tenants behind it: each answers only its own tokens, the gateway reaches no
Acquirium, only TLS 1.2 or later (and no gateway without a certificate),
CORS for the tenant with a UI origin, a database password and tokens the app
supplied (and a password changed), a client generated from
`docs/schemas/openapi.json` (with `openapi-python-client`) runs the smoke
study and the RO surrogate sweep and follows them by their progress streams,
an idle tenant stops and starts again, and backups that are off do nothing.

## For production

- Point pgBackRest and the tenant backups at a real S3 bucket off the host:
  change `repo1-s3-endpoint`, the keys and the CA in
  `$FLEXORCH_SECRETS/cluster/pgbackrest.conf` and `s3.env`, and drop the
  `seaweedfs` service.
- Keep `$FLEXORCH_SECRETS/cluster/backup_pass` somewhere safe as well: without
  it no backup can be read.
- Postgres is reachable only on the tenants' database networks; publish no
  ports.
