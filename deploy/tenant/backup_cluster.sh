#!/usr/bin/env bash
# The cluster's pgBackRest backup (PLAN.md D14, M6), nightly from cron:
#
#   ./backup_cluster.sh [full|diff|incr]      # incr by default
#
# With continuous WAL archiving, any tenant can be restored to any time after
# the oldest kept full backup (restore_tenant.sh). Then every tenant gets its
# own backup (backup_tenant.sh), so one can be restored or moved on its own.
source "$(dirname "$0")/lib.sh"
cluster_ready

type="${1:-incr}"
docker exec "$PG" pgbackrest --stanza="$STANZA" backup --type="$type" >/dev/null
for tenant in $(admin_sql -tA <<<"SELECT rolname FROM pg_roles r
    JOIN pg_database d ON d.datname = r.rolname ORDER BY 1"); do
    if [[ -n "$(tenant_compose "$tenant" ps -q acquirium 2>/dev/null)" ]]; then
        bash "$HERE/backup_tenant.sh" "$tenant" >/dev/null
    fi
done
log "cluster backed up ($type)"
