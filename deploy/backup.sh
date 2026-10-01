#!/bin/sh
# Nightly Postgres backup into ./backups (custom format, restore with pg_restore; see docs/DEPLOY.md).
# Keeps BACKUP_KEEP_DAYS days. Copy ./backups off the server too: a backup on the same disk is not a backup.
set -eu
KEEP="${BACKUP_KEEP_DAYS:-14}"
EVERY="${BACKUP_INTERVAL_SECONDS:-86400}"
mkdir -p /backups
while true; do
  f="/backups/servicedesk-$(date -u +%Y%m%dT%H%M%SZ).dump"
  if pg_dump --format=custom --file="$f.part"; then
    mv "$f.part" "$f"
    echo "$(date -u +%FT%TZ) backup ok $f $(du -h "$f" | cut -f1)"
  else
    rm -f "$f.part"
    echo "$(date -u +%FT%TZ) backup FAILED" >&2
  fi
  find /backups -name 'servicedesk-*.dump' -mtime +"$KEEP" -delete
  sleep "$EVERY"
done
