#!/usr/bin/env bash
# Backup when running on Supabase: the rikz schema of the database, every
# stored statement file and the config. Run from the repository directory:
#   15 2 * * *  cd /srv/rikz && scripts/backup-supabase.sh >> /var/log/rikz-backup.log 2>&1
# Supabase also keeps its own backups (daily on paid plans); this copy is yours.
set -euo pipefail
BACKUP_DIR=${BACKUP_DIR:-/srv/rikz-backups}
KEEP_DAYS=${KEEP_DAYS:-60}
COMPOSE="docker compose -f docker-compose.supabase.yml"
stamp=$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p "$BACKUP_DIR"
umask 077

# Read only the two settings needed (the .env file also holds JSON keys that a
# shell should not evaluate).
env_value() { grep -E "^$1=" .env | head -1 | cut -d= -f2- | sed -e 's/^"//' -e 's/"$//'; }
DATABASE_URL=$(env_value DATABASE_URL)
SCHEMA=$(env_value DATABASE_SCHEMA); SCHEMA=${SCHEMA:-rikz}
[ -n "$DATABASE_URL" ] || { echo "DATABASE_URL missing from .env" >&2; exit 1; }
# pg_dump wants a plain libpq URL (no SQLAlchemy driver name).
PGURL=${DATABASE_URL/postgresql+psycopg:/postgresql:}
docker run --rm -e PGURL="$PGURL" -e SCHEMA="$SCHEMA" postgres:16-alpine \
  sh -c 'pg_dump "$PGURL" --schema="$SCHEMA" --format=custom' > "$BACKUP_DIR/db-$stamp.dump"
docker run --rm -i postgres:16-alpine pg_restore --list < "$BACKUP_DIR/db-$stamp.dump" > /dev/null

$COMPOSE run --rm --no-deps -T -v "$BACKUP_DIR:/backup" web rikz export-store "/backup/files-$stamp"
tar -C "$BACKUP_DIR" -czf "$BACKUP_DIR/files-$stamp.tar.gz" "files-$stamp" && rm -rf "${BACKUP_DIR:?}/files-$stamp"
tar -czf "$BACKUP_DIR/config-$stamp.tar.gz" config

find "$BACKUP_DIR" -type f -mtime +"$KEEP_DAYS" -delete
echo "$(date -u) backup ok: $stamp"
