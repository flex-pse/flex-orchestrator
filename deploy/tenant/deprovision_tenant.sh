#!/usr/bin/env bash
# Remove a tenant (PLAN.md D14, M6), after a final backup.
#
#   ./deprovision_tenant.sh <tenant>
#
# 1. A final backup (backup_tenant.sh), if its stack is running.
# 2. Its stack, volumes and networks.
# 3. Its database and role, then its secret.
# The cluster's pgBackRest backups and the tenant backups are kept, so it can
# be restored later (restore_tenant.sh).
source "$(dirname "$0")/lib.sh"
cluster_ready

tenant="${1:?usage: deprovision_tenant.sh <tenant>}"
check_tenant "$tenant"

if [[ -n "$(tenant_compose "$tenant" ps -q acquirium 2>/dev/null)" ]]; then
    bash "$HERE/backup_tenant.sh" "$tenant" >/dev/null
fi
tenant_compose "$tenant" --profile run down --volumes --remove-orphans >/dev/null 2>&1 || true
for volume in "flexorch_${tenant}_data" "flexorch_${tenant}_scratch"; do
    docker volume rm "$volume" >/dev/null 2>&1 || true
done
if docker network inspect "flexorch_${tenant}_db" >/dev/null 2>&1; then
    docker network disconnect -f "flexorch_${tenant}_db" "$PG" >/dev/null 2>&1 || true
    docker network rm "flexorch_${tenant}_db" >/dev/null
fi
admin_sql <<SQL
DROP DATABASE IF EXISTS "$tenant" WITH (FORCE);
DROP ROLE IF EXISTS "$tenant";
SQL
rm -rf "${FLEXORCH_SECRETS:?}/tenants/$tenant"
log "$tenant is deprovisioned (its backups are kept)"
