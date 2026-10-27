#!/usr/bin/env bash
# Set up the shared cluster once, then start it (PLAN.md D14, M6).
#
#   ./init_cluster.sh
#
# The first run writes the cluster's secrets to $FLEXORCH_SECRETS/cluster:
# the Postgres admin password, the S3 keys, the backup passphrase (pgBackRest
# encrypts its repository with it, and the tenant backups use it too), and a
# CA with the S3 server's TLS certificate. Then, every run: start the cluster,
# create the bucket and the pgBackRest stanza if they're missing, and take a
# full backup if there's none. Safe to run again.
source "$(dirname "$0")/lib.sh"

dir="$FLEXORCH_SECRETS/cluster"
if [[ ! -f "$dir/pgbackrest.conf" ]]; then
    log "writing the cluster's secrets to $dir"
    key="$(random_secret 20)"
    secret="$(random_secret 40)"
    random_secret 40 | write_secret "$dir/postgres_password"
    random_secret 64 | write_secret "$dir/backup_pass"
    write_secret "$dir/s3.json" <<EOF
{"identities": [{"name": "flexorch",
  "credentials": [{"accessKey": "$key", "secretKey": "$secret"}],
  "actions": ["Admin", "Read", "Write", "List", "Tagging"]}]}
EOF
    write_secret "$dir/s3.env" <<EOF
AWS_ACCESS_KEY_ID=$key
AWS_SECRET_ACCESS_KEY=$secret
AWS_DEFAULT_REGION=us-east-1
EOF
    write_secret "$dir/pgbackrest.conf" <<EOF
[global]
repo1-type=s3
repo1-s3-endpoint=flexorch-s3
repo1-storage-port=8333
repo1-s3-bucket=$BUCKET
repo1-s3-region=us-east-1
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
    # A CA and the S3 server's certificate, made with the Postgres image's
    # openssl, as uid 1000 so the files are the stack's.
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
        rm -f s3.csr s3.ext ca.srl
        chmod 644 ca.crt'
fi

log "starting the cluster"
cluster_ready

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
