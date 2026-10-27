#!/usr/bin/env bash
# Create a tenant and start its stack (PLAN.md D14, M6).
#
#   ./provision_tenant.sh <tenant> [--no-start]
#
# 1. A role and a database named <tenant>: the role isn't a superuser and
#    can't create databases or roles; PUBLIC loses every privilege on the
#    database and only the role may connect; the admin creates timescaledb.
# 2. A random password, written only to the tenant's pgpass secret file.
# 3. The tenant's database network, joined by the cluster's Postgres.
# 4. The stack (its volumes and internal network come with it), unless
#    --no-start: restore_tenant.sh fills the volume first.
#
# Running it again for an existing tenant changes nothing but starts the stack.
source "$(dirname "$0")/lib.sh"
cluster_ready

tenant="${1:?usage: provision_tenant.sh <tenant> [--no-start]}"
check_tenant "$tenant"
start=1
[[ "${2:-}" == "--no-start" ]] && start=0
secret="$FLEXORCH_SECRETS/tenants/$tenant/pgpass"

exists="$(admin_sql -tA <<<"SELECT 1 FROM pg_roles WHERE rolname = '$tenant'")"
if [[ -z "$exists" ]]; then
    log "creating role and database $tenant"
    password="$(random_secret 40)"
    # The password goes to psql on stdin, never on a command line.
    admin_sql <<SQL
CREATE ROLE "$tenant" LOGIN PASSWORD '$password'
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
CREATE DATABASE "$tenant" OWNER "$tenant";
REVOKE ALL ON DATABASE "$tenant" FROM PUBLIC;
GRANT CONNECT, TEMPORARY ON DATABASE "$tenant" TO "$tenant";
SQL
    admin_sql -d "$tenant" <<SQL
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA public TO "$tenant";
CREATE EXTENSION IF NOT EXISTS timescaledb;
SQL
    printf '%s\n' "flexorch-pg:5432:$tenant:$tenant:$password" | write_secret "$secret"
    unset password
fi
[[ -f "$secret" ]] || die "$tenant exists but its secret $secret is missing; re-provision it"
# The API's token hashes (api_token.sh adds them); empty until then, and a
# file, so Docker never mounts a directory in its place.
tokens="$FLEXORCH_SECRETS/tenants/$tenant/api_tokens"
[[ -f "$tokens" ]] || : | write_secret "$tokens"

if ! docker network inspect "flexorch_${tenant}_db" >/dev/null 2>&1; then
    docker network create --internal "flexorch_${tenant}_db" >/dev/null
fi
if ! docker network inspect "flexorch_${tenant}_db" \
    --format '{{range .Containers}}{{.Name}} {{end}}' | grep -qw "$PG"; then
    docker network connect "flexorch_${tenant}_db" "$PG"
fi

if [[ "$start" == 1 ]]; then
    log "starting $tenant's stack"
    tenant_compose "$tenant" up -d --wait acquirium
fi
log "$tenant is provisioned"
