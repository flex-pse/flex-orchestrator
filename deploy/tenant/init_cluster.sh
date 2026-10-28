#!/usr/bin/env bash
# Set up the shared cluster once, then start it (PLAN.md D14, M6).
#
#   ./init_cluster.sh                       # backups on (or as last recorded)
#   FLEXORCH_BACKUPS=off ./init_cluster.sh  # no backups: no SeaweedFS, no WAL archive
#
# The cluster's secrets are files in $FLEXORCH_SECRETS/cluster. The app (or
# operator) may supply any of them before the first run, and this uses them;
# it generates only the ones that are missing:
#
#   postgres_password  the Postgres admin's password
#   backup_pass        the passphrase pgBackRest encrypts its repository with
#                      (the tenant backups use it too)
#   s3.env             AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY and
#                      AWS_DEFAULT_REGION: the S3 store's keys
#   ca.crt, s3.crt, s3.key
#                      the CA pgBackRest trusts, and the S3 server's
#                      certificate. Internal: only pgBackRest and the backup
#                      scripts see it, on the cluster network.
#   s3.json, pgbackrest.conf
#                      the S3 server's and pgBackRest's configuration, written
#                      from the above when missing (supply pgbackrest.conf to
#                      use a real S3)
#
# Then, every run: start the cluster, create the bucket and the pgBackRest
# stanza if they're missing, and take a full backup if there's none. Safe to
# run again.
#
# FLEXORCH_BACKUPS is recorded in $FLEXORCH_SECRETS/settings/cluster.env, so
# every script follows it. Run this again with the other value to switch:
# Postgres restarts with WAL archiving on or off. With backups off, the backup
# scripts do nothing and say so, restore_tenant.sh refuses, and
# deprovision_tenant.sh takes no final backup.
source "$(dirname "$0")/lib.sh"

backups=on
backups_on || backups=off
mkdir -p "$SETTINGS_DIR"
printf 'FLEXORCH_BACKUPS=%s\n' "$backups" >"$CLUSTER_SETTINGS"

dir="$FLEXORCH_SECRETS/cluster"
mkdir -p "$dir"
generating() { log "generating $1 (none supplied in $dir)"; }
if [[ ! -f "$dir/postgres_password" ]]; then
    generating postgres_password
    random_secret 40 | write_secret "$dir/postgres_password"
fi
if [[ ! -f "$dir/backup_pass" ]]; then
    generating backup_pass
    random_secret 64 | write_secret "$dir/backup_pass"
fi
if [[ ! -f "$dir/s3.env" ]]; then
    generating "the S3 keys (s3.env)"
    write_secret "$dir/s3.env" <<EOF
AWS_ACCESS_KEY_ID=$(random_secret 20)
AWS_SECRET_ACCESS_KEY=$(random_secret 40)
AWS_DEFAULT_REGION=us-east-1
EOF
fi
s3_setting() { sed -n "s/^$1=//p" "$dir/s3.env"; }
key="$(s3_setting AWS_ACCESS_KEY_ID)"
secret="$(s3_setting AWS_SECRET_ACCESS_KEY)"
region="$(s3_setting AWS_DEFAULT_REGION)"
check_secret_value "s3.env's AWS_ACCESS_KEY_ID" "$key"
check_secret_value "s3.env's AWS_SECRET_ACCESS_KEY" "$secret"
check_secret_value backup_pass "$(cat "$dir/backup_pass")"
[[ "${region:-us-east-1}" =~ ^[a-z0-9-]+$ ]] || die "s3.env's AWS_DEFAULT_REGION isn't a region"
if [[ ! -f "$dir/s3.json" ]]; then
    write_secret "$dir/s3.json" <<EOF
{"identities": [{"name": "flexorch",
  "credentials": [{"accessKey": "$key", "secretKey": "$secret"}],
  "actions": ["Admin", "Read", "Write", "List", "Tagging"]}]}
EOF
fi
if [[ ! -f "$dir/pgbackrest.conf" ]]; then
    write_secret "$dir/pgbackrest.conf" <<EOF
[global]
repo1-type=s3
repo1-s3-endpoint=flexorch-s3
repo1-storage-port=8333
repo1-s3-bucket=$BUCKET
repo1-s3-region=${region:-us-east-1}
repo1-s3-uri-style=path
repo1-s3-key=$key
repo1-s3-key-secret=$secret
repo1-storage-ca-file=/etc/flexorch/ca.crt
repo1-path=/pgbackrest
repo1-cipher-type=aes-256-cbc
repo1-cipher-pass=$(cat "$dir/backup_pass")
repo1-retention-full=4
start-fast=y
process-max=2
log-level-console=info
log-level-file=off

[$STANZA]
pg1-path=/home/postgres/pgdata/data
pg1-socket-path=/var/run/postgresql
EOF
fi
unset key secret
if [[ ! -f "$dir/ca.crt" || ! -f "$dir/s3.crt" || ! -f "$dir/s3.key" ]]; then
    # A CA and the S3 server's certificate, made with the Postgres image's
    # openssl, as uid 1000 so the files are the stack's. Internal only: the S3
    # store is on the cluster network, and pgBackRest needs TLS to reach it.
    # The CA's key is thrown away: it signs this one certificate.
    generating "the S3 store's CA and certificate"
    docker run --rm --user "$SECRET_UID:$SECRET_UID" -v "$dir:/out" -w /out \
        --entrypoint bash "${PG_IMAGE:-timescale/timescaledb-ha:pg17.6-ts2.22.0}" -c '
        set -e
        umask 077
        openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
            -keyout ca.key -out ca.crt -subj /CN=flexorch-backup-ca 2>/dev/null
        openssl req -newkey rsa:2048 -nodes -keyout s3.key -out s3.csr \
            -subj /CN=flexorch-s3 2>/dev/null
        printf "subjectAltName=DNS:flexorch-s3\n" > s3.ext
        openssl x509 -req -in s3.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
            -days 825 -extfile s3.ext -out s3.crt 2>/dev/null
        rm -f s3.csr s3.ext ca.srl ca.key
        chmod 644 ca.crt'
fi

log "starting the cluster (backups $backups)"
cluster_ready
if [[ "$backups" == off ]]; then
    log "cluster ready; backups are off (FLEXORCH_BACKUPS=on ./init_cluster.sh to turn them on)"
    exit 0
fi

if ! s3 s3api head-bucket --bucket "$BUCKET" >/dev/null 2>&1; then
    log "creating bucket $BUCKET"
    s3 s3 mb "s3://$BUCKET" >/dev/null
fi
# A fresh SeaweedFS (3.97) can list the first bucket's objects but not read
# them until its S3 gateway restarts, so check a write reads back.
echo ok | s3 s3 cp - "s3://$BUCKET/.probe" >/dev/null
if ! s3 s3 cp "s3://$BUCKET/.probe" - >/dev/null 2>&1; then
    log "restarting the S3 server so it sees the new bucket"
    docker restart flexorch-s3 >/dev/null
    cluster_ready
    for attempt in $(seq 10); do
        s3 s3 cp "s3://$BUCKET/.probe" - >/dev/null 2>&1 && break
        [[ "$attempt" == 10 ]] && die "the S3 bucket can't be read"
        sleep 2
    done
fi
s3 s3 rm "s3://$BUCKET/.probe" >/dev/null

docker exec "$PG" pgbackrest --stanza="$STANZA" stanza-create >/dev/null
docker exec "$PG" pgbackrest --stanza="$STANZA" check >/dev/null
if ! docker exec "$PG" pgbackrest --stanza="$STANZA" info --output=json \
    | grep -q '"backup":\[{'; then
    log "taking the first full backup"
    docker exec "$PG" pgbackrest --stanza="$STANZA" backup --type=full >/dev/null
fi
log "cluster ready"
