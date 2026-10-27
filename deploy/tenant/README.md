# Tenant stacks

One stack per tenant (a group or project) on a shared Postgres cluster
(PLAN.md D14). A tenant gets its own database and role, password, volume,
networks and containers; nothing is shared between tenants but the cluster,
which each tenant reaches only as its own role.

```
              ┌──────────── flexorch_<tenant> (internal) ────────────┐
              │ orchestrator (run on demand) ──► acquirium :8000      │
              └───────────────────────────────────────┬──────────────┘
                                   flexorch_<tenant>_db │ (internal)
   flexorch_cluster (internal):  postgres (TimescaleDB, pgBackRest) ──► seaweedfs (S3, TLS)
```

## What's here

| File | What it does |
| --- | --- |
| `compose.cluster.yaml` | Postgres 17 + TimescaleDB with WAL archiving, and SeaweedFS as the S3 store backups go to |
| `compose.tenant.yaml` | one tenant's stack: its Acquirium, and the orchestrator container scripts run in |
| `pg_hba.conf` | a role may log in only to the database of its own name; the admin only over the socket |
| `init_cluster.sh` | first run: secrets, CA and TLS certificate, bucket, pgBackRest stanza, first full backup |
| `provision_tenant.sh <t>` | role, database, password, database network; then starts the stack |
| `deprovision_tenant.sh <t>` | a final backup, then removes the stack, volumes, networks, database, role and secret |
| `backup_tenant.sh <t>` | a consistent dump and volume copy (and a graph export), encrypted to S3 |
| `backup_cluster.sh [full\|diff\|incr]` | the nightly pgBackRest backup, then every tenant's |
| `restore_tenant.sh <t> <time\|id> --into <new>` | point-in-time (or from a tenant backup) restore into a new tenant |
| `smoke.py` | a short study a tenant's orchestrator container runs, for checking a stack |
| `api_token.sh <t> <user> <role>` | a token for the tenant's HTTP API, printed once; only its hash is kept |

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
- `FLEXORCH_BACKUP_BUCKET`, `PG_IMAGE`, `S3_IMAGE`, `AWS_CLI_IMAGE`,
  `DOCKER_COMPOSE`.

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

A tenant's API (`flex-orch serve`, PLAN.md M7) is the `api` service, started
on demand:

```bash
./api_token.sh tenant_a alice editor            # prints the token once
TENANT=tenant_a docker compose -f compose.tenant.yaml -p flexorch-tenant_a \
    --profile api up -d --wait api
```

It's on the tenant's network only (no published port; a gateway comes in
M8), so clients there use `Orchestrator(url="http://api:8080", token=...)`.
Tokens are viewer, editor or owner. `api_token.sh` keeps only each token's
SHA-256, in `$FLEXORCH_SECRETS/tenants/<t>/api_tokens`; delete a line and
restart `api` to revoke one. With an identity provider instead, set
`FLEXORCH_OIDC_ISSUER` and `FLEXORCH_OIDC_AUDIENCE`: JWTs must carry the
tenant's name in a `tenant` claim and a `role` claim.

## Secrets

`init_cluster.sh` writes the cluster's secrets to `$FLEXORCH_SECRETS/cluster`
(mode 600): the Postgres admin password, the S3 keys, the backup passphrase,
and a CA with the S3 server's certificate. `provision_tenant.sh` writes each
tenant's password to `$FLEXORCH_SECRETS/tenants/<t>/pgpass`, mounted only into
that tenant's Acquirium as a libpq password file. The password is never in an
environment variable, a command line, a DSN or a log; users and the
orchestrator never see it. Re-provision a tenant (deprovision, restore) to
rotate it.

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

## Tests

```bash
FLEXORCH_TEST_DEPLOY=1 pytest src/flex_orchestrator/tests/test_deploy.py
```

On Windows, with Docker in WSL: `FLEXORCH_TEST_DEPLOY="wsl.exe -d Ubuntu --"`.
They provision two fresh tenants on the cluster and check isolation (database,
network, volume, secret, hardening), run the smoke study in mode 3, and run the
restore drill; the tenants are removed afterwards.

## For production

- Point pgBackRest and the tenant backups at a real S3 bucket off the host:
  change `repo1-s3-endpoint`, the keys and the CA in
  `$FLEXORCH_SECRETS/cluster/pgbackrest.conf` and `s3.env`, and drop the
  `seaweedfs` service.
- Keep `$FLEXORCH_SECRETS/cluster/backup_pass` somewhere safe as well: without
  it no backup can be read.
- Postgres is reachable only on the tenants' database networks; publish no
  ports.
