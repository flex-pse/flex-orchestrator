#!/usr/bin/env bash
# Back up one tenant at one point (PLAN.md D14, M6): its database and its
# volume, consistent with each other, plus an export of its graph.
#
#   ./backup_tenant.sh <tenant>        # prints the backup's id, e.g. 20261026T230000Z
#
# The tenant's Acquirium is stopped for the dump and the volume copy, so the
# two match, then started again. Everything is encrypted with the backup
# passphrase and uploaded to s3://<bucket>/tenants/<tenant>/<id>/:
#
#   db.dump.enc      pg_dump -Fc of the tenant's database
#   volume.tar.enc   the Acquirium data_dir volume (graph, configs)
#   graph.ttl.enc    the graph as Turtle (GET /export_graph), taken first
#   manifest.json    the tenant, the time and the WAL position
#
# The cluster's pgBackRest backups and WAL archive (point-in-time recovery)
# are separate: backup_cluster.sh. With the cluster's backups off, or the
# tenant's (provision_tenant.sh --tenant-backups off), it does nothing and
# prints no id.
source "$(dirname "$0")/lib.sh"
if ! tenant_backups_on "${1:?usage: backup_tenant.sh <tenant>}"; then
    log "backups are off for $1 (or this cluster); it isn't backed up"
    exit 0
fi
cluster_ready

tenant="${1:?usage: backup_tenant.sh <tenant>}"
check_tenant "$tenant"
id="$(date -u +%Y%m%dT%H%M%SZ)"
prefix="s3://$BUCKET/tenants/$tenant/$id"
# Running and healthy first: the graph export needs it.
tenant_compose "$tenant" up -d --wait acquirium >/dev/null 2>&1     || die "$tenant's Acquirium won't start; is it provisioned?"
acquirium="$(tenant_compose "$tenant" ps -q acquirium)"

log "backing up $tenant as $id"
docker exec "$acquirium" python -c "
import shutil, sys, urllib.request
with urllib.request.urlopen('http://127.0.0.1:8000/export_graph', timeout=600) as r:
    shutil.copyfileobj(r, sys.stdout.buffer)" | encrypt | s3 s3 cp - "$prefix/graph.ttl.enc" >/dev/null

tenant_compose "$tenant" stop acquirium >/dev/null
restart() { tenant_compose "$tenant" up -d --wait acquirium >/dev/null; }
trap restart EXIT

point="$(admin_sql -tA <<<"SELECT now() AT TIME ZONE 'UTC', pg_current_wal_lsn()")"
docker exec "$PG" pg_dump -Fc -U postgres -d "$tenant" | encrypt \
    | s3 s3 cp - "$prefix/db.dump.enc" >/dev/null
docker run --rm --network none -v "flexorch_${tenant}_data:/data:ro" \
    --entrypoint tar "$FLEXORCH_IMAGE" -C /data -cf - . | encrypt \
    | s3 s3 cp - "$prefix/volume.tar.enc" >/dev/null

restart
trap - EXIT
printf '{"tenant": "%s", "id": "%s", "at": "%s", "wal_lsn": "%s"}\n' \
    "$tenant" "$id" "${point%|*}" "${point#*|}" | s3 s3 cp - "$prefix/manifest.json" >/dev/null
echo "$id"
