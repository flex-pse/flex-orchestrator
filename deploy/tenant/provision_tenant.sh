#!/usr/bin/env bash
# Create a tenant and start its stack (PLAN.md D14, M6).
#
#   ./provision_tenant.sh <tenant> [--no-start] [--idle-minutes N]
#                         [--tenant-backups on|off] [--cors-origins LIST]
#                         [--password-file FILE|-] [--oidc-issuer URL]
#                         [--oidc-audience AUD] [--oidc-jwks-url URL]
#
# 1. A role and a database named <tenant>: the role isn't a superuser and
#    can't create databases or roles; PUBLIC loses every privilege on the
#    database and only the role may connect; the admin creates timescaledb.
# 2. Its password, written only to the tenant's pgpass secret file: the one in
#    --password-file (its first line; - reads stdin), so the app that governs
#    the secrets makes it, else a random one. At least 16 letters, digits or
#    ._~+/=-. Given for an existing tenant, it changes the role's password.
# 3. The tenant's database network, joined by the cluster's Postgres, and its
#    edge network and route, for the gateway (deploy/gateway).
# 4. Its settings, in $FLEXORCH_SECRETS/settings/tenant_<t>.env:
#    --idle-minutes 0 (the default) keeps it running until it's stopped; N
#    stops the API after N minutes with no requests, and idle_tenants.sh then
#    stops Acquirium (start_tenant.sh starts both). --tenant-backups off: it
#    isn't backed up. --cors-origins https://ui.example.org,...: the browser
#    origins its API serves, for a UI on another origin than the gateway.
#    --oidc-issuer, --oidc-audience (and --oidc-jwks-url, if the issuer's
#    discovered one isn't right): the identity provider whose JWTs its API
#    accepts, besides api_token.sh's tokens. Without them, the environment's
#    FLEXORCH_OIDC_* when the stack starts. An empty value clears one.
# 5. The stack's Acquirium (its volumes and internal network come with it),
#    unless --no-start: restore_tenant.sh fills the volume first.
#
# Running it again for an existing tenant changes only the settings given, and
# starts Acquirium.
source "$(dirname "$0")/lib.sh"

usage="usage: provision_tenant.sh <tenant> [--no-start] [--idle-minutes N] \
[--tenant-backups on|off] [--cors-origins LIST] [--password-file FILE|-] \
[--oidc-issuer URL] [--oidc-audience AUD] [--oidc-jwks-url URL]"
tenant="${1:?$usage}"
shift
check_tenant "$tenant"
start=1
password_file=""
idle="$(tenant_setting "$tenant" FLEXORCH_IDLE_MINUTES 0)"
backups="$(tenant_setting "$tenant" FLEXORCH_TENANT_BACKUPS on)"
cors="$(tenant_setting "$tenant" FLEXORCH_CORS_ORIGINS "")"
issuer="$(tenant_setting "$tenant" FLEXORCH_OIDC_ISSUER "")"
audience="$(tenant_setting "$tenant" FLEXORCH_OIDC_AUDIENCE "")"
jwks="$(tenant_setting "$tenant" FLEXORCH_OIDC_JWKS_URL "")"
while [[ $# -gt 0 ]]; do
    case "$1" in
        --no-start) start=0 ;;
        --idle-minutes) idle="${2:?$usage}"; shift ;;
        --tenant-backups) backups="${2:?$usage}"; shift ;;
        --cors-origins) cors="${2?$usage}"; shift ;;
        --password-file) password_file="${2:?$usage}"; shift ;;
        --oidc-issuer) issuer="${2?$usage}"; shift ;;
        --oidc-audience) audience="${2?$usage}"; shift ;;
        --oidc-jwks-url) jwks="${2?$usage}"; shift ;;
        *) die "$usage" ;;
    esac
    shift
done
[[ "$idle" =~ ^[0-9]+$ ]] || die "--idle-minutes is a whole number of minutes (0: never)"
[[ "$backups" =~ ^(on|off)$ ]] || die "--tenant-backups is on or off"
IFS=, read -ra origins <<<"$cors"
for origin in "${origins[@]}"; do
    [[ "$origin" =~ ^https?://[A-Za-z0-9.-]+(:[0-9]+)?$ ]] \
        || die "--cors-origins lists origins like https://ui.example.org, comma-separated"
done
for url in "$issuer" "$jwks"; do
    [[ -z "$url" || "$url" =~ ^https?://[A-Za-z0-9.:/_~%-]+$ ]] \
        || die "--oidc-issuer and --oidc-jwks-url are URLs like https://id.example.org/realms/flex"
done
[[ "$audience" =~ ^[A-Za-z0-9.:/_~%-]*$ ]] || die "--oidc-audience may use letters, digits and .:/_~%-"
[[ -n "$issuer" || -z "$audience$jwks" ]] || die "--oidc-audience and --oidc-jwks-url need --oidc-issuer"
[[ -z "$issuer" || -n "$audience" ]] || die "--oidc-issuer needs --oidc-audience"
secret="$FLEXORCH_SECRETS/tenants/$tenant/pgpass"
password=""
if [[ -n "$password_file" ]]; then
    if [[ "$password_file" == - ]]; then
        IFS= read -r password || true
    else
        [[ -f "$password_file" ]] || die "--password-file: $password_file isn't a file"
        IFS= read -r password <"$password_file" || true
    fi
    check_secret_value "the password in --password-file" "$password"
fi
# Only now: its checks run containers that would read stdin, the password.
cluster_ready

exists="$(admin_sql -tA <<<"SELECT 1 FROM pg_roles WHERE rolname = '$tenant'")"
if [[ -z "$exists" ]]; then
    log "creating role and database $tenant"
    password="${password:-$(random_secret 40)}"
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
elif [[ -n "$password" ]]; then
    # Acquirium reads the password file for each new connection; the ones it
    # has open stay open.
    log "changing $tenant's password"
    admin_sql <<<"ALTER ROLE \"$tenant\" PASSWORD '$password'"
    printf '%s\n' "flexorch-pg:5432:$tenant:$tenant:$password" | write_secret "$secret"
fi
unset password
[[ -f "$secret" ]] || die "$tenant exists but its secret $secret is missing; re-provision it"
# The API's token hashes (api_token.sh adds them); empty until then, and a
# file, so Docker never mounts a directory in its place.
tokens="$FLEXORCH_SECRETS/tenants/$tenant/api_tokens"
[[ -f "$tokens" ]] || : | write_secret "$tokens"
# Settings, not secrets: the scripts read them as whoever runs them.
mkdir -p "$SETTINGS_DIR"
printf '%s\n' "FLEXORCH_IDLE_MINUTES=$idle" "FLEXORCH_TENANT_BACKUPS=$backups" \
    "FLEXORCH_CORS_ORIGINS=$cors" "FLEXORCH_OIDC_ISSUER=$issuer" \
    "FLEXORCH_OIDC_AUDIENCE=$audience" "FLEXORCH_OIDC_JWKS_URL=$jwks" \
    >"$SETTINGS_DIR/tenant_$tenant.env"

if ! docker network inspect "flexorch_${tenant}_db" >/dev/null 2>&1; then
    docker network create --internal "flexorch_${tenant}_db" >/dev/null
fi
if ! docker network inspect "flexorch_${tenant}_db" \
    --format '{{range .Containers}}{{.Name}} {{end}}' | grep -qw "$PG"; then
    docker network connect "flexorch_${tenant}_db" "$PG"
fi
edge_network "$tenant"
gateway_route "$tenant"

if [[ "$start" == 1 ]]; then
    log "starting $tenant's stack"
    tenant_compose "$tenant" up -d --wait acquirium
fi
log "$tenant is provisioned (idle minutes: $idle, tenant backups: $backups)"
