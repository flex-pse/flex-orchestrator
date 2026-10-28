#!/usr/bin/env bash
# Stop a tenant's API and Acquirium; its data, settings and route are kept
# (PLAN.md M8). start_tenant.sh starts it again.
#
#   ./stop_tenant.sh <tenant>
#
# Its Acquirium's drivers stop with it. While it's stopped, the gateway answers
# its route with 502.
source "$(dirname "$0")/lib.sh"

tenant="${1:?usage: stop_tenant.sh <tenant>}"
check_tenant "$tenant"
tenant_compose "$tenant" --profile api stop api acquirium >/dev/null 2>&1 || true
log "$tenant is stopped"
