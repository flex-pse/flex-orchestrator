#!/usr/bin/env bash
# Start the gateway in front of the tenants' APIs (PLAN.md D15, M8).
#
#   ./init_gateway.sh --cert FILE --key FILE  # HTTPS with this certificate
#   ./init_gateway.sh --https                 # HTTPS with the one already installed
#   ./init_gateway.sh --plain-http            # plain HTTP, on 127.0.0.1 only
#   ./init_gateway.sh                         # as the last run chose
#
# The certificate is the app's: this never makes one. --cert and --key install
# it (with its chain) as $FLEXORCH_SECRETS/gateway/tls.crt and tls.key; or the
# app writes those files itself, readable by uid 1000, and runs --https. Run
# it again after a renewal: Traefik reads the files when the gateway restarts.
#
# --plain-http is for a gateway that only this machine uses (a demo, a local
# app), or one behind the app's own TLS proxy: it publishes its ports on
# 127.0.0.1, unless FLEXORCH_GATEWAY_BIND says otherwise. Tokens cross it in
# the clear, so never publish it on a network without TLS in front.
#
# Settings (environment):
#   FLEXORCH_GATEWAY_HOST        the name clients use, for the log (default localhost)
#   FLEXORCH_GATEWAY_HTTPS_PORT  the published HTTPS port (default 443)
#   FLEXORCH_GATEWAY_HTTP_PORT   the published HTTP port (default 80): with
#                                HTTPS it only redirects; with --plain-http it
#                                serves the routes
#   FLEXORCH_GATEWAY_BIND        the address the ports are published on
#                                (default 0.0.0.0 with HTTPS, 127.0.0.1 without)
#
# The choice is recorded in $FLEXORCH_SECRETS/settings/gateway.env. Then it
# starts the gateway, writes every provisioned tenant's route, and joins every
# tenant's edge network. Safe to run again (after a host restart, or a new
# certificate).
source "$(dirname "$0")/../tenant/lib.sh"
GATEWAY_HERE="$(cd "$(dirname "$0")" && pwd)"
host="${FLEXORCH_GATEWAY_HOST:-localhost}"
https_port="${FLEXORCH_GATEWAY_HTTPS_PORT:-443}"
http_port="${FLEXORCH_GATEWAY_HTTP_PORT:-80}"

usage="usage: init_gateway.sh [--cert FILE --key FILE | --https | --plain-http]"
cert="" key="" tls=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --cert) cert="${2:?$usage}"; shift ;;
        --key) key="${2:?$usage}"; shift ;;
        --https) tls=on ;;
        --plain-http) tls=off ;;
        *) die "$usage" ;;
    esac
    shift
done
[[ -z "$cert" && -z "$key" ]] || [[ -n "$cert" && -n "$key" && "$tls" != off ]] \
    || die "$usage"
[[ -z "$cert" ]] || tls=on
if [[ -z "$tls" && -f "$GATEWAY_SETTINGS" ]]; then
    tls="$(sed -n 's/^FLEXORCH_GATEWAY_TLS=//p' "$GATEWAY_SETTINGS")"
fi
[[ -n "$tls" ]] || die "no certificate given: $usage"

mkdir -p "$GATEWAY_DIR" "$ROUTES_DIR"
chmod 700 "$GATEWAY_DIR"
if [[ "$(id -u)" != "$SECRET_UID" ]]; then
    sudo chown "$SECRET_UID:$SECRET_UID" "$GATEWAY_DIR"
fi
if [[ -n "$cert" ]]; then
    install_secret "$cert" "$GATEWAY_DIR/tls.crt"
    install_secret "$key" "$GATEWAY_DIR/tls.key"
fi
if [[ "$tls" == on ]]; then
    [[ -s "$GATEWAY_DIR/tls.crt" && -s "$GATEWAY_DIR/tls.key" ]] \
        || die "no certificate in $GATEWAY_DIR (tls.crt, tls.key): $usage"
    bind="${FLEXORCH_GATEWAY_BIND:-0.0.0.0}"
else
    bind="${FLEXORCH_GATEWAY_BIND:-127.0.0.1}"
    [[ "$bind" == 127.0.0.1 ]] \
        || log "warning: plain HTTP on $bind; tokens cross it in the clear"
fi
mkdir -p "$SETTINGS_DIR"
printf 'FLEXORCH_GATEWAY_TLS=%s\n' "$tls" >"$GATEWAY_SETTINGS"

cp "$GATEWAY_HERE/common.yml" "$ROUTES_DIR/common.yml"
if [[ "$tls" == on ]]; then
    # The certificate, TLS 1.2 or later, HSTS (on HTTPS routes, gateway_route),
    # and plain HTTP redirected to the published HTTPS port.
    cat >"$ROUTES_DIR/tls.yml" <<EOF
# Written by init_gateway.sh: HTTPS with the supplied certificate.
tls:
  stores:
    default:
      defaultCertificate:
        certFile: /etc/flexorch/gateway/tls.crt
        keyFile: /etc/flexorch/gateway/tls.key
  options:
    default:
      minVersion: VersionTLS12

http:
  routers:
    to-https:
      rule: "PathPrefix(\`/\`)"
      entryPoints: [web]
      middlewares: [to-https]
      service: noop@internal
  middlewares:
    to-https:
      redirectScheme:
        scheme: https
        port: "$https_port"
        permanent: true
    hsts:
      headers:
        stsSeconds: 31536000
EOF
else
    rm -f "$ROUTES_DIR/tls.yml"
fi
shopt -s nullglob
for dir in "$FLEXORCH_SECRETS"/tenants/*/; do
    gateway_route "$(basename "$dir")"
done
# Down first, then up: a new certificate is read at start, and a port moved to
# another address isn't free until the old container is gone (Docker Desktop
# frees it a moment later, hence the retries).
gateway_compose() {
    FLEXORCH_GATEWAY_BIND="$bind" compose -f "$GATEWAY_HERE/compose.gateway.yaml" "$@"
}
gateway_compose down >/dev/null 2>&1 || true
for attempt in $(seq 5); do
    gateway_compose up -d --wait >/dev/null 2>&1 && break
    [[ "$attempt" == 5 ]] && die "the gateway isn't healthy; see docker logs $GATEWAY"
    sleep 3
done
for dir in "$FLEXORCH_SECRETS"/tenants/*/; do
    edge_network "$(basename "$dir")"
done
if [[ "$tls" == on ]]; then
    log "gateway ready: https://$host:$https_port/<tenant>/"
else
    log "gateway ready: http://$host:$http_port/<tenant>/ (plain HTTP, on $bind)"
fi
