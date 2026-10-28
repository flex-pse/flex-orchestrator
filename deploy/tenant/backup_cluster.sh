#!/usr/bin/env bash
# The cluster's pgBackRest backup (PLAN.md D14, M6), nightly from cron:
#
#   ./backup_cluster.sh [full|diff|incr]      # incr by default
#
# With continuous WAL archiving, any tenant can be restored to any time after
# the oldest kept full backup (restore_tenant.sh). Then every tenant gets its
# own backup (backup_tenant.sh), so one can be restored or moved on its own,
# except tenants provisioned with --tenant-backups off.
#
# With the cluster's backups off (init_cluster.sh), it does nothing.
source "$(dirname "$0")/lib.sh"
if ! backups_on; then
    log "backups are off for this cluster; nothing to do"
    exit 0
fi
cluster_ready

type="${1:-incr}"
docker exec "$PG" pgbackrest --stanza="$STANZA" backup --type="$type" >/dev/null
for tenant in $(admin_sql -tA <<<"SELECT rolname FROM pg_roles r
    JOIN pg_database d ON d.datname = r.rolname ORDER BY 1"); do
    if ! tenant_backups_on "$tenant"; then
        log "skipping $tenant (--tenant-backups off)"
    elif [[ -n "$(tenant_compose "$tenant" ps -q acquirium 2>/dev/null)" ]]; then
        bash "$HERE/backup_tenant.sh" "$tenant" >/dev/null
    fi
done
log "cluster backed up ($type)"
