#!/usr/bin/env bash
# Restore a tenant into a new tenant (PLAN.md D14, M6).
#
#   ./restore_tenant.sh <tenant> <time | backup id> --into <new tenant>
#
# <time> is UTC, e.g. "2026-10-26 23:30:00": the database is brought back to
# that moment from the cluster's pgBackRest backups and WAL archive, in a
# throwaway Postgres, and dumped from there. <backup id> (from
# backup_tenant.sh) takes the database from that backup's dump instead.
#
# Either way the volume (graph and configs) comes from the latest tenant
# backup at or before that point, the new tenant is provisioned and filled,
# its stack started, and Acquirium's metadata graph synced from the database.
# Run `flex-orch verify --against <earlier report>` in the new stack after.
#
# The source tenant needn't exist any more; the new one mustn't exist yet.
# With the cluster's backups off there's nothing to restore from.
source "$(dirname "$0")/lib.sh"
backups_on || die "backups are off for this cluster (init_cluster.sh); there's nothing to restore"
cluster_ready

usage="usage: restore_tenant.sh <tenant> <time | backup id> --into <new tenant>"
tenant="${1:?$usage}"
target="${2:?$usage}"
[[ "${3:-}" == "--into" ]] || die "$usage"
new="${4:?$usage}"
check_tenant "$tenant"
check_tenant "$new"
[[ -z "$(admin_sql -tA <<<"SELECT 1 FROM pg_roles WHERE rolname = '$new'")" ]] \
    || die "tenant $new exists; restore into a new name"

# The tenant backups, oldest first; ids sort as times.
ids="$(s3 s3 ls "s3://$BUCKET/tenants/$tenant/" | awk '/PRE/ {sub("/", "", $2); print $2}')"
[[ -n "$ids" ]] || die "there are no backups of $tenant"
if [[ "$target" =~ ^[0-9]{8}T[0-9]{6}Z$ ]]; then
    grep -qx "$target" <<<"$ids" || die "$tenant has no backup $target"
    snapshot="$target"
    time=""
else
    time="$target"
    cutoff="$(date -u -d "$time" +%Y%m%dT%H%M%SZ)" || die "can't read the time '$time'"
    snapshot="$(awk -v c="$cutoff" '$0 <= c' <<<"$ids" | tail -1)"
    [[ -n "$snapshot" ]] || die "$tenant has no backup at or before $time"
fi
prefix="s3://$BUCKET/tenants/$tenant/$snapshot"
log "restoring $tenant into $new: volume from backup $snapshot, database from ${time:-that backup}"

work="$(mktemp -d)"
cleanup_helper() { :; }
trap 'cleanup_helper; rm -rf "$work"' EXIT

if [[ -z "$time" ]]; then
    s3 s3 cp "$prefix/db.dump.enc" - | decrypt >"$work/db.dump"
else
    # Point-in-time recovery of only this database, in a throwaway Postgres.
    docker exec "$PG" pgbackrest --stanza="$STANZA" check >/dev/null # WAL so far archived
    helper="flexorch-restore-$new"
    volume="flexorch_restore_$new"
    cleanup_helper() {
        docker rm -f "$helper" >/dev/null 2>&1 || true
        docker volume rm "$volume" >/dev/null 2>&1 || true
    }
    secrets=(
        -v "$FLEXORCH_SECRETS/cluster/pgbackrest.conf:/etc/flexorch/pgbackrest.conf:ro"
        -v "$FLEXORCH_SECRETS/cluster/ca.crt:/etc/flexorch/ca.crt:ro"
        -e PGBACKREST_CONFIG=/etc/flexorch/pgbackrest.conf
    )
    image="$(docker inspect "$PG" --format '{{.Config.Image}}')"
    docker volume create "$volume" >/dev/null
    pitr() {
        docker run --rm --network "$CLUSTER_NETWORK" -v "$volume:/home/postgres/pgdata" \
            "${secrets[@]}" --entrypoint pgbackrest "$image" --stanza="$STANZA" \
            --type=time "--target=$time+00" --target-action=promote "$@" restore
    }
    # Only this database if the backup set has it; a database created since
    # the last backup needs the whole cluster restored (pgBackRest error 80).
    if ! pitr --db-include="$tenant" >"$work/pitr.log" 2>&1; then
        grep -q "ERROR: \[080\]" "$work/pitr.log" || die "$(cat "$work/pitr.log")"
        log "$tenant is newer than the last full backup; restoring the whole cluster"
        pitr >/dev/null
    fi
    docker run -d --name "$helper" --network "$CLUSTER_NETWORK" \
        -v "$volume:/home/postgres/pgdata" "${secrets[@]}" "$image" \
        postgres -c archive_mode=off -c listen_addresses= >/dev/null
    for _ in $(seq 120); do
        state="$(docker exec "$helper" psql -X -tA -U postgres \
            -c "SELECT pg_is_in_recovery()" 2>/dev/null || true)"
        [[ "$state" == f ]] && break
        sleep 2
    done
    [[ "$state" == f ]] || die "the point-in-time recovery didn't finish; see docker logs $helper"
    docker exec "$helper" pg_dump -Fc -U postgres -d "$tenant" >"$work/db.dump"
    cleanup_helper
fi

bash "$HERE/provision_tenant.sh" "$new" --no-start
# Timescale's catalog names the source role as its jobs' owner, so the role
# must exist while the dump loads; if the tenant is gone, a stand-in without a
# login does, and is dropped once the jobs belong to the new tenant.
standin=0
if [[ -z "$(admin_sql -tA <<<"SELECT 1 FROM pg_roles WHERE rolname = '$tenant'")" ]]; then
    admin_sql <<<"CREATE ROLE \"$tenant\" NOLOGIN;"
    standin=1
fi
# Timescale's restore procedure, as the admin; then the new tenant owns it all.
admin_sql -d "$new" <<<"SELECT timescaledb_pre_restore();" >/dev/null
docker exec -i "$PG" pg_restore -U postgres -d "$new" --no-owner --no-privileges \
    --exit-on-error <"$work/db.dump"
admin_sql -d "$new" >/dev/null <<SQL
SELECT timescaledb_post_restore();
UPDATE _timescaledb_config.bgw_job SET owner = '"$new"'::regrole
    WHERE owner = '"$tenant"'::regrole;
DO \$\$
DECLARE r record;
BEGIN
    FOR r IN
        SELECT c.oid::regclass AS name, c.relkind FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f')
    LOOP
        EXECUTE format('ALTER %s %s OWNER TO %I',
            CASE r.relkind WHEN 'S' THEN 'SEQUENCE' WHEN 'v' THEN 'VIEW'
                WHEN 'm' THEN 'MATERIALIZED VIEW' WHEN 'f' THEN 'FOREIGN TABLE'
                ELSE 'TABLE' END,
            r.name, '$new');
    END LOOP;
END
\$\$;
SQL
if [[ "$standin" == 1 ]]; then
    admin_sql <<<"DROP ROLE \"$tenant\";"
fi

tenant_compose "$new" create acquirium >/dev/null 2>&1
s3 s3 cp "$prefix/volume.tar.enc" - | decrypt | docker run --rm -i --network none \
    -v "flexorch_${new}_data:/data" --entrypoint tar "$FLEXORCH_IMAGE" -C /data -xf -
tenant_compose "$new" up -d --wait acquirium >/dev/null
docker exec "$(tenant_compose "$new" ps -q acquirium)" python -c "
import urllib.request
urllib.request.urlopen(urllib.request.Request(
    'http://127.0.0.1:8000/experiments/metadata/sync', method='POST'), timeout=600)"
log "restored $tenant into $new"
