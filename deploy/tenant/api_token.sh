#!/usr/bin/env bash
# Give someone a token for a tenant's HTTP API (PLAN.md D15, M7).
#
#   ./api_token.sh <tenant> <user> <viewer|editor|owner>               # prints a new token once
#   ./api_token.sh <tenant> <user> <viewer|editor|owner> --sha256 HEX  # one the app made
#
# Only the token's SHA-256 is kept, in $FLEXORCH_SECRETS/tenants/<t>/api_tokens,
# which the tenant's api service reads (FLEXORCH_API_TOKENS_FILE); it's
# restarted to pick the new token up. With --sha256, the app that governs the
# secrets makes the token and passes only its hash (64 hex digits), so the
# token never reaches this host. To revoke a token, delete its line and run
# this script's restart: `tenant_compose <t> --profile api restart api`.
# With an identity provider instead (FLEXORCH_OIDC_ISSUER), tokens come from it.
source "$(dirname "$0")/lib.sh"

usage="usage: api_token.sh <tenant> <user> <viewer|editor|owner> [--sha256 HEX]"
tenant="${1:?$usage}"
user="${2:?$usage}"
role="${3:?$usage}"
digest=""
if [[ $# -gt 3 ]]; then
    [[ "$4" == --sha256 && $# -eq 5 ]] || die "$usage"
    digest="${5,,}"
    [[ "$digest" =~ ^[0-9a-f]{64}$ ]] || die "--sha256 is the token's SHA-256, 64 hex digits"
fi
check_tenant "$tenant"
[[ "$user" =~ ^[A-Za-z0-9_.@-]+$ ]] || die "user '$user' may use letters, digits and _.@-"
[[ "$role" =~ ^(viewer|editor|owner)$ ]] || die "role must be viewer, editor or owner"
file="$FLEXORCH_SECRETS/tenants/$tenant/api_tokens"
[[ -f "$file" ]] || die "$tenant isn't provisioned (no $file)"

token=""
if [[ -z "$digest" ]]; then
    token="$(random_secret 48)"
    digest="$(printf %s "$token" | sha256sum | cut -d' ' -f1)"
fi
printf '%s:%s:sha256:%s\n' "$user" "$role" "$digest" >>"$file"
if [[ -n "$(tenant_compose "$tenant" --profile api ps -q api 2>/dev/null)" ]]; then
    tenant_compose "$tenant" --profile api restart api >/dev/null 2>&1
fi
if [[ -n "$token" ]]; then
    log "a $role token for $user on $tenant (keep it: it isn't stored)"
    echo "$token"
else
    log "$user's $role token on $tenant is registered"
fi
