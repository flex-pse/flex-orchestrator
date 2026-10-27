#!/usr/bin/env bash
# Give someone a token for a tenant's HTTP API (PLAN.md D15, M7).
#
#   ./api_token.sh <tenant> <user> <viewer|editor|owner>    # prints the token once
#
# Only the token's SHA-256 is kept, in $FLEXORCH_SECRETS/tenants/<t>/api_tokens,
# which the tenant's api service reads (FLEXORCH_API_TOKENS_FILE); it's
# restarted to pick the new token up. To revoke a token, delete its line and
# run this script's restart: `tenant_compose <t> --profile api restart api`.
# With an identity provider instead (FLEXORCH_OIDC_ISSUER), tokens come from it.
source "$(dirname "$0")/lib.sh"

tenant="${1:?usage: api_token.sh <tenant> <user> <viewer|editor|owner>}"
user="${2:?usage: api_token.sh <tenant> <user> <viewer|editor|owner>}"
role="${3:?usage: api_token.sh <tenant> <user> <viewer|editor|owner>}"
check_tenant "$tenant"
[[ "$user" =~ ^[A-Za-z0-9_.@-]+$ ]] || die "user '$user' may use letters, digits and _.@-"
[[ "$role" =~ ^(viewer|editor|owner)$ ]] || die "role must be viewer, editor or owner"
file="$FLEXORCH_SECRETS/tenants/$tenant/api_tokens"
[[ -f "$file" ]] || die "$tenant isn't provisioned (no $file)"

token="$(random_secret 48)"
digest="$(printf %s "$token" | sha256sum | cut -d' ' -f1)"
printf '%s:%s:sha256:%s\n' "$user" "$role" "$digest" >>"$file"
if [[ -n "$(tenant_compose "$tenant" --profile api ps -q api 2>/dev/null)" ]]; then
    tenant_compose "$tenant" --profile api restart api >/dev/null 2>&1
fi
log "a $role token for $user on $tenant (keep it: it isn't stored)"
echo "$token"
