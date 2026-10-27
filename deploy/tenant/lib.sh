# Shared by the deploy/tenant scripts; sourced, not run.
#
# Settings (environment):
#   FLEXORCH_SECRETS  where secrets live (default ~/.flexorch/secrets). Must be
#                     on a filesystem with real permissions (not /mnt/<drive>
#                     on WSL): libpq ignores a password file others can read.
#   FLEXORCH_IMAGE    the flex-orchestrator image (default flexorch:dev)
#   FLEXORCH_BACKUP_BUCKET  the backup bucket (default flexorch-backups)
#   AWS_CLI_IMAGE     the image tenant backups upload with

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export FLEXORCH_SECRETS="${FLEXORCH_SECRETS:-$HOME/.flexorch/secrets}"
export FLEXORCH_IMAGE="${FLEXORCH_IMAGE:-flexorch:dev}"
BUCKET="${FLEXORCH_BACKUP_BUCKET:-flexorch-backups}"
AWS_CLI_IMAGE="${AWS_CLI_IMAGE:-amazon/aws-cli:2.27.50}"
PG=flexorch-pg
CLUSTER_NETWORK=flexorch_cluster
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

# Wait until the cluster is up and healthy (after a host restart, its
# containers come back on their own, but not at once).
cluster_ready() {
    compose -f "$HERE/compose.cluster.yaml" up -d --wait >/dev/null 2>&1 \
        || die "the cluster isn't healthy; see docker ps"
    # SeaweedFS's health check is its master; the S3 port can come up later.
    for _ in $(seq 30); do
        s3 s3 ls >/dev/null 2>&1 && return
        sleep 2
    done
    die "the S3 server doesn't answer"
}

tenant_project() { echo "flexorch-$1"; }

tenant_compose() {
    local tenant="$1"
    shift
    TENANT="$tenant" compose -f "$HERE/compose.tenant.yaml" \
        -p "$(tenant_project "$tenant")" "$@"
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
