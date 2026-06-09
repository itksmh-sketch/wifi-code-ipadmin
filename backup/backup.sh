#!/bin/sh
set -eu

ts="$(date +%Y%m%d_%H%M%S)"
out="/backups/hotspot_${ts}.sql.gz"

if PGPASSWORD="${POSTGRES_PASSWORD}" pg_dump -h "${POSTGRES_HOST}" -p "${POSTGRES_PORT}" -U "${POSTGRES_USER}" "${POSTGRES_DB}" | gzip > "${out}"; then
  echo "event=backup_success file=${out}"
else
  echo "event=backup_failed file=${out}" >&2
  exit 1
fi

ls -1t /backups/hotspot_*.sql.gz 2>/dev/null | awk 'NR>7' | xargs -r rm -f
