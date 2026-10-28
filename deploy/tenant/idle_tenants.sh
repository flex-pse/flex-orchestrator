#!/usr/bin/env bash
# Stop the Acquirium of each tenant that timed out (PLAN.md M8); from cron,
# every few minutes:
#
#   */5 * * * *  /path/to/deploy/tenant/idle_tenants.sh
#
# A tenant provisioned with --idle-minutes N runs its API with
# FLEXORCH_IDLE_MINUTES=N: the API stops itself after N minutes with no
# requests and no job. Once it has, and no orchestrator container of the
# tenant is running a script, this stops its Acquirium too. Persistent tenants
# (--idle-minutes 0) are never touched. start_tenant.sh starts both again.
source "$(dirname "$0")/lib.sh"

shopt -s nullglob
for settings in "$SETTINGS_DIR"/tenant_*.env; do
    tenant="$(basename "$settings" .env)"
    tenant="${tenant#tenant_}"
    [[ "$(tenant_setting "$tenant" FLEXORCH_IDLE_MINUTES 0)" == 0 ]] && continue
    [[ -n "$(tenant_compose "$tenant" ps -q --status running acquirium 2>/dev/null)" ]] \
        || continue
    [[ -z "$(tenant_compose "$tenant" --profile api ps -q --status running api 2>/dev/null)" ]] \
        || continue
    # `compose ps` doesn't list `run` containers; their labels do.
    [[ -z "$(docker ps -q --filter "label=com.docker.compose.project=$(tenant_project "$tenant")" \
        --filter label=com.docker.compose.service=orchestrator)" ]] || continue
    tenant_compose "$tenant" stop acquirium >/dev/null 2>&1
    log "stopped $tenant's Acquirium: its API timed out"
done
