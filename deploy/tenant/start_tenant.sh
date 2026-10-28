#!/usr/bin/env bash
# Start a provisioned tenant: its Acquirium and its API (PLAN.md M8).
#
#   ./start_tenant.sh <tenant>
#
# For a tenant that times out (provision_tenant.sh --idle-minutes N), the
# control plane runs this when a user of the tenant logs in; for a persistent
# one, after it was stopped by hand. Waits until both are healthy, so the
# gateway's route answers when it returns.
source "$(dirname "$0")/lib.sh"
cluster_ready

tenant="${1:?usage: start_tenant.sh <tenant>}"
check_tenant "$tenant"
[[ -f "$FLEXORCH_SECRETS/tenants/$tenant/pgpass" ]] || die "$tenant isn't provisioned"
edge_network "$tenant"
tenant_compose "$tenant" --profile api up -d --wait acquirium api >/dev/null 2>&1 \
    || die "$tenant's stack didn't start; see docker ps -a"
log "$tenant is running"
