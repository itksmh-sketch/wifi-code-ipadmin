#!/bin/sh
# Nightly database dump. A run counts as a success only if pg_dump exited 0,
# the gzip stream is intact and the dump ends with pg_dump's completion
# marker. Until then it is written under a .partial name, so a failed run
# never leaves a file that looks like a backup, and retention (which only
# runs after a success) never rotates out good copies.
set -eu
set -o pipefail

ts="$(date +%Y%m%d_%H%M%S)"
out="/backups/hotspot_${ts}.sql.gz"
tmp="${out}.partial"

fail() {
  rm -f "${tmp}"
  echo "event=backup_failed file=${out} reason=$1" >&2
  exit 1
}

# Leftovers of a run that was killed part-way (container stop, VM shutdown).
rm -f /backups/*.partial

PGPASSWORD="${POSTGRES_PASSWORD}" pg_dump -h "${POSTGRES_HOST}" -p "${POSTGRES_PORT}" -U "${POSTGRES_USER}" "${POSTGRES_DB}" \
  | gzip > "${tmp}" || fail pg_dump
gzip -t "${tmp}" || fail gzip_corrupt
tail_lines="$(zcat "${tmp}" | tail -n 8)" || fail read_back
case "${tail_lines}" in
  *"PostgreSQL database dump complete"*) ;;
  *) fail incomplete_dump ;;
esac

mv "${tmp}" "${out}"
echo "event=backup_success file=${out} bytes=$(wc -c < "${out}")"

ls -1t /backups/hotspot_*.sql.gz 2>/dev/null | awk 'NR>7' | xargs -r rm -f
