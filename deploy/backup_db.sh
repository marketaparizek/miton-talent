#!/usr/bin/env bash
#
# Daily backup of the miton_talent database (small, holds personal data, so
# daily and 30 days of retention, unlike Alister's biweekly cadence).
#
#   bash deploy/backup_db.sh            dump now, prune old dumps, run OFFSITE_CMD if set
#
# Cron (installed by setup_server.sh):
#   15 3 * * * cd /root/live/miton-talent && bash deploy/backup_db.sh >> logs/backup.log 2>&1
#
# Restore:
#   sudo -u postgres pg_restore -d miton_talent --clean --if-exists <file>.dump
#
# Env (from .env or the shell): BACKUP_DIR, RETENTION_DAYS, DB_NAME, OFFSITE_CMD.
set -euo pipefail

if [[ -f .env ]]; then set -a; source .env; set +a; fi

BACKUP_DIR="${BACKUP_DIR:-/root/db_backups/miton_talent}"
RETENTION_DAYS="${RETENTION_DAYS:-30}"
DB_NAME="${DB_NAME:-miton_talent}"
mkdir -p "$BACKUP_DIR"

stamp="$(date +%Y%m%d_%H%M%S)"
out="$BACKUP_DIR/${DB_NAME}_${stamp}.dump"

if sudo -u postgres pg_dump -Fc "$DB_NAME" > "$out"; then
  echo "$(date -Is) backup ok $(du -h "$out" | cut -f1) $out"
else
  rm -f "$out"
  echo "$(date -Is) backup FAILED" >&2
  exit 1
fi

find "$BACKUP_DIR" -name "${DB_NAME}_*.dump" -mtime +"$RETENTION_DAYS" -delete

if [[ -n "${OFFSITE_CMD:-}" ]]; then
  bash -c "$OFFSITE_CMD" && echo "$(date -Is) offsite copy ok" || echo "$(date -Is) offsite copy FAILED" >&2
fi
