#!/usr/bin/env bash
# Start the gateway in front of the tenants' APIs (PLAN.md D15, M8).
#
#   ./init_gateway.sh
#
# Settings (environment):
#   FLEXORCH_GATEWAY_HOST        the name clients use (default localhost); the
#                                generated certificate is for it
#   FLEXORCH_GATEWAY_HTTPS_PORT  the published HTTPS port (default 443)
#   FLEXORCH_GATEWAY_HTTP_PORT   the published HTTP port, which only redirects
#                                (default 80)
#
# TLS: put your own certificate and key in $FLEXORCH_SECRETS/gateway/tls.crt
# and tls.key first. If they aren't there, this makes a CA and a certificate
# for FLEXORCH_GATEWAY_HOST (for air-gapped sites and tests); clients trust
# $FLEXORCH_SECRETS/gateway/ca.crt.
#
# Then it starts the gateway, writes every provisioned tenant's route, and
# joins every tenant's edge network. Safe to run again (after a host restart,
# or a new certificate).
source "$(dirname "$0")/../tenant/lib.sh"
GATEWAY_HERE="$(cd "$(dirname "$0")" && pwd)"
host="${FLEXORCH_GATEWAY_HOST:-localhost}"

mkdir -p "$GATEWAY_DIR" "$ROUTES_DIR"
chmod 700 "$GATEWAY_DIR"
if [[ "$(id -u)" != "$SECRET_UID" ]]; then
    sudo chown "$SECRET_UID:$SECRET_UID" "$GATEWAY_DIR"
fi
if [[ ! -f "$GATEWAY_DIR/tls.crt" ]]; then
    log "making a CA and a certificate for $host"
    docker run --rm --user "$SECRET_UID:$SECRET_UID" -v "$GATEWAY_DIR:/out" -w /out \
        -e HOST="$host" --entrypoint bash \
        "${PG_IMAGE:-timescale/timescaledb-ha:pg17.6-ts2.22.0}" -c '
        set -e
        umask 077
        openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
            -keyout ca.key -out ca.crt -subj /CN=flexorch-gateway-ca 2>/dev/null
        openssl req -newkey rsa:2048 -nodes -keyout tls.key -out tls.csr \
            -subj "/CN=$HOST" 2>/dev/null
        printf "subjectAltName=DNS:%s,DNS:localhost,IP:127.0.0.1\n" "$HOST" > tls.ext
        openssl x509 -req -in tls.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
            -days 825 -extfile tls.ext -out tls.crt 2>/dev/null
        rm -f tls.csr tls.ext ca.srl
        chmod 644 ca.crt tls.crt'
fi

cp "$GATEWAY_HERE/common.yml" "$ROUTES_DIR/common.yml"
shopt -s nullglob
for dir in "$FLEXORCH_SECRETS"/tenants/*/; do
    gateway_route "$(basename "$dir")"
done
compose -f "$GATEWAY_HERE/compose.gateway.yaml" up -d --wait >/dev/null 2>&1 \
    || die "the gateway isn't healthy; see docker logs $GATEWAY"
for dir in "$FLEXORCH_SECRETS"/tenants/*/; do
    edge_network "$(basename "$dir")"
done
log "gateway ready: https://$host:${FLEXORCH_GATEWAY_HTTPS_PORT:-443}/<tenant>/"
