#!/usr/bin/env bash
# Logical Postgres backup (complements EBS snapshots; portable to any cloud).
#   deploy/scripts/backup_db.sh [dest_dir]          # cron: 30 21 * * * /opt/mitra/deploy/scripts/backup_db.sh
# Optional: MITRA_BACKUP_S3=s3://bucket/prefix uploads the dump (any S3-compatible target via
# AWS_ENDPOINT_URL). Keeps 7 local dumps.
set -euo pipefail
DEST="${1:-/var/backups/mitra}"
mkdir -p "$DEST"
cd "$(dirname "$0")/.."
f="$DEST/mitra-$(date -u +%Y%m%dT%H%M%SZ).sql.gz"
docker compose exec -T postgres pg_dump -U mitra mitra | gzip > "$f"
chmod 600 "$f"
ls -1t "$DEST"/mitra-*.sql.gz | tail -n +8 | xargs -r rm --
if [ -n "${MITRA_BACKUP_S3:-}" ]; then aws s3 cp "$f" "$MITRA_BACKUP_S3/" --only-show-errors; fi
echo "$f"
