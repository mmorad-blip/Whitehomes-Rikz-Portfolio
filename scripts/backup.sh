#!/usr/bin/env bash
# Nightly backup of the database, the statement file store and the config.
# Run from the repository directory on the server, e.g. from cron:
#   15 2 * * *  cd /srv/rikz && scripts/backup.sh >> /var/log/rikz-backup.log 2>&1
# Copy BACKUP_DIR off the server (e.g. with rclone) – a backup on the same
# disk does not survive losing the server.
set -euo pipefail
BACKUP_DIR=${BACKUP_DIR:-/srv/rikz-backups}
KEEP_DAYS=${KEEP_DAYS:-60}
stamp=$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p "$BACKUP_DIR"
umask 077

docker compose exec -T db pg_dump -U rikz -d rikz --format=custom > "$BACKUP_DIR/db-$stamp.dump"
docker compose run --rm --no-deps -T --entrypoint tar worker -C /data -cz files > "$BACKUP_DIR/files-$stamp.tar.gz"
tar -czf "$BACKUP_DIR/config-$stamp.tar.gz" config

# Check the dump is readable before trusting it.
docker compose exec -T db pg_restore --list < "$BACKUP_DIR/db-$stamp.dump" > /dev/null

find "$BACKUP_DIR" -type f -mtime +"$KEEP_DAYS" -delete
echo "$(date -u) backup ok: $stamp"
