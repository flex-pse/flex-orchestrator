# Shared by the deploy/tenant scripts; sourced, not run.
#
# Settings (environment):
#   FLEXORCH_SECRETS  where secrets live (default ~/.flexorch/secrets). Must be
#                     on a filesystem with real permissions (not /mnt/<drive>
#                     on WSL): libpq ignores a password file others can read.
#   FLEXORCH_IMAGE    the flex-orchestrator image (default flexorch:dev)
#   FLEXORCH_BACKUP_BUCKET  the backup bucket (default flexorch-backups)
#   FLEXORCH_BACKUPS  on or off: init_cluster.sh records it for the cluster;
#                     set it for one command to override that record
#   AWS_CLI_IMAGE     the image tenant backups upload with
#
# Per tenant, $FLEXORCH_SECRETS/settings/tenant_<t>.env (provision_tenant.sh):
#   FLEXORCH_IDLE_MINUTES    0: persistent; N: the API stops after N idle
#                            minutes, and idle_tenants.sh then stops Acquirium
#   FLEXORCH_TENANT_BACKUPS  on or off: whether it's backed up
#   FLEXORCH_CORS_ORIGINS    browser origins its API serves (a UI's), comma-separated

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export FLEXORCH_SECRETS="${FLEXORCH_SECRETS:-$HOME/.flexorch/secrets}"
export FLEXORCH_IMAGE="${FLEXORCH_IMAGE:-flexorch:dev}"
BUCKET="${FLEXORCH_BACKUP_BUCKET:-flexorch-backups}"
AWS_CLI_IMAGE="${AWS_CLI_IMAGE:-amazon/aws-cli:2.27.50}"
PG=flexorch-pg
GATEWAY=flexorch-gateway
CLUSTER_NETWORK=flexorch_cluster
SETTINGS_DIR="$FLEXORCH_SECRETS/settings" # not secret: readable by whoever runs the scripts
CLUSTER_SETTINGS="$SETTINGS_DIR/cluster.env"
GATEWAY_DIR="$FLEXORCH_SECRETS/gateway" # its TLS key and certificate
ROUTES_DIR="$SETTINGS_DIR/gateway"     # its routes (not secret); Traefik watches it
STANZA=flexorch
# Every container a stack runs is uid 1000 (deploy/docker/Dockerfile and
# timescaledb-ha), so secret files belong to it.
SECRET_UID=1000

log() { echo "flexorch: $*" >&2; }
die() { log "$*"; exit 1; }

# Docker Compose v2: $DOCKER_COMPOSE, else `docker compose`, else the plugin
# binary itself (a stale Docker Desktop link can hide the plugin from docker).
if [[ -z "${DOCKER_COMPOSE:-}" ]]; then
    if docker compose version >/dev/null 2>&1; then
        DOCKER_COMPOSE="docker compose"
    else
        for plugin in /usr/libexec/docker/cli-plugins/docker-compose \
            /usr/lib/docker/cli-plugins/docker-compose; do
            if "$plugin" version >/dev/null 2>&1; then
                DOCKER_COMPOSE="$plugin"
                break
            fi
        done
    fi
fi
[[ -n "${DOCKER_COMPOSE:-}" ]] || die "Docker Compose v2 isn't installed"
compose() { $DOCKER_COMPOSE "$@"; }

check_tenant() {
    [[ "$1" =~ ^[a-z][a-z0-9_]{1,30}$ ]] \
        || die "tenant name '$1' must be lowercase letters, digits and _, 2-31 long"
    [[ "$1" != postgres && "$1" != template* ]] || die "'$1' is reserved"
}

# A random secret of letters and digits (nothing that needs quoting). It reads
# a fixed 1 KiB, so no pipe is cut short (which pipefail would report).
random_secret() {
    local chars
    chars="$(head -c 1024 /dev/urandom | LC_ALL=C tr -dc 'A-Za-z0-9')"
    printf %s "${chars:0:${1:-40}}"
}

# Write stdin to a file only uid 1000 can read.
write_secret() {
    local path="$1"
    mkdir -p "$(dirname "$path")"
    chmod 700 "$(dirname "$path")"
    (umask 077 && cat >"$path")
    chmod 600 "$path"
    if [[ "$(id -u)" != "$SECRET_UID" ]]; then
        sudo chown "$SECRET_UID:$SECRET_UID" "$path" "$(dirname "$path")"
    fi
}

# psql as the cluster admin, over the container's socket. SQL comes on stdin,
# so nothing secret is ever on a command line.
admin_sql() { docker exec -i "$PG" psql -X -q -v ON_ERROR_STOP=1 -U postgres "$@"; }

# Whether the cluster backs up: FLEXORCH_BACKUPS for this command, else what
# init_cluster.sh recorded, else on.
backups_on() {
    local value="${FLEXORCH_BACKUPS:-}"
    if [[ -z "$value" && -f "$CLUSTER_SETTINGS" ]]; then
        value="$(sed -n 's/^FLEXORCH_BACKUPS=//p' "$CLUSTER_SETTINGS")"
    fi
    case "${value:-on}" in
        on) return 0 ;;
        off) return 1 ;;
        *) die "FLEXORCH_BACKUPS is on or off, not '$value'" ;;
    esac
}

# The cluster's compose command: SeaweedFS (the backup store) and WAL
# archiving only when backups are on.
cluster_compose() {
    if backups_on; then
        FLEXORCH_ARCHIVE_MODE=on compose -f "$HERE/compose.cluster.yaml" \
            --profile backups "$@"
    else
        FLEXORCH_ARCHIVE_MODE=off compose -f "$HERE/compose.cluster.yaml" "$@"
    fi
}

# Wait until the cluster is up and healthy (after a host restart, its
# containers come back on their own, but not at once).
cluster_ready() {
    cluster_compose up -d --wait >/dev/null 2>&1 \
        || die "the cluster isn't healthy; see docker ps"
    backups_on || return 0
    # SeaweedFS's health check is its master; the S3 port can come up later.
    for _ in $(seq 30); do
        s3 s3 ls >/dev/null 2>&1 && return
        sleep 2
    done
    die "the S3 server doesn't answer"
}

tenant_project() { echo "flexorch-$1"; }

# A tenant's setting from its tenant.env, or the default given.
tenant_setting() {
    local file="$SETTINGS_DIR/tenant_$1.env" value=""
    [[ -f "$file" ]] && value="$(sed -n "s/^$2=//p" "$file")"
    printf %s "${value:-$3}"
}

# Whether a tenant is backed up: the cluster's backups are on, and it didn't
# opt out (provision_tenant.sh --tenant-backups off).
tenant_backups_on() {
    backups_on && [[ "$(tenant_setting "$1" FLEXORCH_TENANT_BACKUPS on)" == on ]]
}

tenant_compose() {
    local tenant="$1" idle restart=unless-stopped cors
    shift
    cors="$(tenant_setting "$tenant" FLEXORCH_CORS_ORIGINS "${FLEXORCH_CORS_ORIGINS:-}")"
    idle="$(tenant_setting "$tenant" FLEXORCH_IDLE_MINUTES 0)"
    # An API that stops itself when idle mustn't be restarted for it.
    if [[ "$idle" == 0 ]]; then idle=""; else restart=on-failure; fi
    TENANT="$tenant" FLEXORCH_IDLE_MINUTES="$idle" FLEXORCH_API_RESTART="$restart" \
        FLEXORCH_CORS_ORIGINS="$cors" compose -f "$HERE/compose.tenant.yaml" -p "$(tenant_project "$tenant")" "$@"
}

# The tenant's network with the gateway: only its API and the gateway are on
# it, never its Acquirium. Created here, joined by the gateway if it runs.
edge_network() {
    local network="flexorch_$1_edge"
    if ! docker network inspect "$network" >/dev/null 2>&1; then
        docker network create --internal "$network" >/dev/null
    fi
    if docker inspect "$GATEWAY" >/dev/null 2>&1 && ! docker network inspect "$network" \
        --format '{{range .Containers}}{{.Name}} {{end}}' | grep -qw "$GATEWAY"; then
        docker network connect "$network" "$GATEWAY"
    fi
}

# The gateway's route to a tenant's API: https://<host>/<tenant>/... goes to
# its api container with the prefix stripped, and the API's server puts it
# back (FLEXORCH_ROOT_PATH), so its schema and docs name it. Traefik watches
# the folder.
gateway_route() {
    local tenant="$1"
    mkdir -p "$ROUTES_DIR"
    cat >"$ROUTES_DIR/tenant_$tenant.yml" <<EOF
# Written by provision_tenant.sh: $tenant's API behind the gateway.
http:
  routers:
    $tenant:
      rule: "PathPrefix(\`/$tenant/\`) || Path(\`/$tenant\`)"
      entryPoints: [websecure]
      service: $tenant
      middlewares: [$tenant-prefix, secure-headers]
      tls: {}
  middlewares:
    $tenant-prefix:
      stripPrefix:
        prefixes: ["/$tenant"]
  services:
    $tenant:
      loadBalancer:
        servers:
          - url: "http://api-$tenant:8080"
EOF
}

# The AWS CLI on the cluster network, pointed at the S3 server, trusting the
# cluster's CA. Reads stdin.
s3() {
    docker run --rm -i --network "$CLUSTER_NETWORK" \
        --env-file "$FLEXORCH_SECRETS/cluster/s3.env" \
        -v "$FLEXORCH_SECRETS/cluster/ca.crt:/ca.crt:ro" \
        "$AWS_CLI_IMAGE" --endpoint-url https://flexorch-s3:8333 \
        --ca-bundle /ca.crt "$@"
}

# Encrypt / decrypt a stream with the backup passphrase (the same one
# pgBackRest encrypts its repository with).
encrypt() {
    docker exec -i "$PG" openssl enc -aes-256-cbc -pbkdf2 -salt \
        -pass file:/etc/flexorch/backup_pass
}
decrypt() {
    docker exec -i "$PG" openssl enc -d -aes-256-cbc -pbkdf2 \
        -pass file:/etc/flexorch/backup_pass
}
