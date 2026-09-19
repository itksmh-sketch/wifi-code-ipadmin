#!/usr/bin/env bash
#
# Router-scoped session identity: migration -> config swap -> FreeRADIUS restart
# -> phase-B migration. Runs end to end with no manual pause.
#
#   ./scripts/deploy_session_id_fix.sh --dry-run    # print the plan, change nothing
#   ./scripts/deploy_session_id_fix.sh              # execute
#
# Ordering is the whole point and is NOT arbitrary:
#   045 is additive, so between it and the restart BOTH the old and new schema
#   contracts are satisfied and the running FreeRADIUS keeps working.
#   046 removes the old contract and must therefore run only AFTER the restart.
#   Applying 046 early breaks every accounting packet.
#
# Expect a few seconds of RADIUS downtime at the restart: in-flight logins fail
# and clients retry. Established sessions are unaffected (enforcement is on the
# router). Run it off-peak.
set -Eeuo pipefail   # -E: ERR trap must be inherited by functions, or rollback never runs

cd "$(dirname "$0")/.."
REPO="$(pwd)"
DRY_RUN="${1:-}"
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP_DIR="${REPO}/.deploy-backups/${STAMP}"

# NOTE: this script was written for the ONE-OFF 2026-09-19 rollout and has already
# been run. Both artefacts it consumes were intentionally removed afterwards so
# nobody edits the wrong copy: freeradius/sql.conf.new (staged config, now merged
# into freeradius/sql.conf) and migrations/pending/046 (now in versions/). The
# preflight below will refuse to run until they are re-derived. Kept in-tree as the
# reviewed record of how the change was sequenced, and as a template for the next
# migration + FreeRADIUS restart that must happen in a fixed order.
PENDING="backend/src/db/migrations/pending/046_drop_global_session_id_key.py"
VERSIONS="backend/src/db/migrations/versions"

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
run()  { if [ "$DRY_RUN" = "--dry-run" ]; then printf '   [dry-run] %s\n' "$*"; else eval "$@"; fi; }
psqlq() { docker exec hotspot-postgres psql -U "${POSTGRES_USER:-hotspot_user}" -d "${POSTGRES_DB:-hotspot_db}" -tAc "$1"; }

# ---------------------------------------------------------------- preflight --
say "Preflight"
for c in hotspot-postgres hotspot-backend hotspot-freeradius hotspot-freeradius-secondary; do
  docker inspect -f '{{.State.Running}}' "$c" | grep -qx true || { echo "FATAL: $c is not running"; exit 1; }
done
[ -f freeradius/sql.conf.new ] || { echo "FATAL: freeradius/sql.conf.new missing"; exit 1; }
[ -f "$PENDING" ]              || { echo "FATAL: $PENDING missing"; exit 1; }
echo "  alembic head before: $(docker exec hotspot-backend alembic current 2>/dev/null | tail -1)"
echo "  sessions rows:       $(psqlq 'SELECT count(*) FROM sessions;')"
echo "  open sessions:       $(psqlq 'SELECT count(*) FROM sessions WHERE stopped_at IS NULL;')"

say "Backup (schema + sessions/vouchers data + current config)"
run "mkdir -p '$BACKUP_DIR'"
run "docker exec hotspot-postgres pg_dump -U '${POSTGRES_USER:-hotspot_user}' -d '${POSTGRES_DB:-hotspot_db}' \
       --schema-only --no-owner > '$BACKUP_DIR/schema.sql'"
run "docker exec hotspot-postgres pg_dump -U '${POSTGRES_USER:-hotspot_user}' -d '${POSTGRES_DB:-hotspot_db}' \
       --data-only --no-owner -t sessions -t vouchers > '$BACKUP_DIR/sessions_vouchers.sql'"
run "cp freeradius/sql.conf '$BACKUP_DIR/sql.conf.bak'"

rollback() {
  echo
  echo "!!! FAILED — rolling back to the pre-deploy state"
  cp "$BACKUP_DIR/sql.conf.bak" freeradius/sql.conf || true
  rm -f "$VERSIONS/046_drop_global_session_id_key.py" || true
  docker compose restart freeradius freeradius-secondary || true
  echo "config restored + FreeRADIUS restarted. 045 is additive and safe to leave applied;"
  echo "to undo it as well: docker exec hotspot-backend alembic downgrade 044_platform_notification_templates"
  echo "DB backup: $BACKUP_DIR"
  exit 1
}
[ "$DRY_RUN" = "--dry-run" ] || trap rollback ERR

# ------------------------------------------------------- 1. migration 045 ----
say "1/6  Apply migration 045 (additive: compound key + last_interim_at + uuid overload)"
run "docker exec hotspot-backend alembic upgrade head"
if [ "$DRY_RUN" != "--dry-run" ]; then
  [ "$(psqlq "SELECT count(*) FROM pg_constraint WHERE conname='uq_sessions_router_session';")" = "1" ] \
    || { echo "FATAL: uq_sessions_router_session not created"; exit 1; }
  [ "$(psqlq "SELECT count(*) FROM pg_proc WHERE proname='update_voucher_usage';")" = "2" ] \
    || { echo "FATAL: expected BOTH update_voucher_usage overloads at this point"; exit 1; }
  echo "  ok: compound key present, both function overloads present"
fi

# --------------------------------------- 2. validate new config off-prod -----
say "2/6  Validate the new sql.conf against the live (now-migrated) schema"
# Uses `docker compose run` rather than raw `docker run`: compose injects
# POSTGRES_* from .env itself, so the DB password is never read, exported or
# echoed by this script. An earlier version fell back to a literal default
# password here and the check failed with a bare exit 1 and no output.
validate_new_config() {
  docker compose run --rm --no-deps \
    -v "${REPO}/freeradius/sql.conf.new:/overrides/sql.conf:ro" \
    --entrypoint /bin/sh freeradius -c '
      ln -sf /etc/freeradius/mods-available/sql /etc/freeradius/mods-enabled/sql
      cp /overrides/sql.conf /etc/freeradius/mods-enabled/sql
      sed -i "s/host=postgres/host=127.0.0.1/" /etc/freeradius/mods-enabled/sql
      cp /overrides/default /etc/freeradius/sites-enabled/default
      cp /overrides/clients.conf /etc/freeradius/clients.conf
      chmod 640 /etc/freeradius/mods-enabled/sql /etc/freeradius/sites-enabled/default /etc/freeradius/clients.conf
      freeradius -C'
}
if [ "$DRY_RUN" = "--dry-run" ]; then
  printf '   [dry-run] docker compose run --rm --no-deps ... freeradius -C (new sql.conf)\n'
else
  validate_new_config || { echo "FATAL: new sql.conf failed validation — nothing swapped"; exit 1; }
fi
echo "  ok: config validates and the sql module instantiates against the real schema"

# ------------------------------------------------------- 3. swap config ------
say "3/6  Swap sql.conf into place"
run "cp freeradius/sql.conf.new freeradius/sql.conf"

# ------------------------------------- 4. restart BOTH FreeRADIUS nodes ------
# SIGHUP does not reload SQL config (read_clients / queries) — a full restart is
# required. Both nodes mount the same sql.conf, so they must move together or
# they will disagree about the schema contract.
say "4/6  Restart primary + secondary FreeRADIUS (full restart, not SIGHUP)"
run "docker compose restart freeradius freeradius-secondary"

say "     Waiting for both to come back"
if [ "$DRY_RUN" != "--dry-run" ]; then
  for c in hotspot-freeradius hotspot-freeradius-secondary; do
    for i in $(seq 1 60); do
      if docker inspect -f '{{.State.Running}}' "$c" 2>/dev/null | grep -qx true; then
        sleep 1
        docker logs "$c" --since 60s 2>&1 | grep -qiE "Failed binding|Errors reading|error.*sql_hotspot" \
          && { echo "FATAL: $c reported config/SQL errors after restart"; exit 1; }
        echo "  $c: up"; break
      fi
      sleep 1
      [ "$i" = "60" ] && { echo "FATAL: $c did not come back"; exit 1; }
    done
  done
fi

# --------------------------------------------------- 5. migration 046 --------
say "5/6  Apply migration 046 (drop the global key + the text overload)"
run "cp '$PENDING' '$VERSIONS/046_drop_global_session_id_key.py'"
run "docker exec hotspot-backend alembic upgrade head"
if [ "$DRY_RUN" != "--dry-run" ]; then
  [ "$(psqlq "SELECT count(*) FROM pg_constraint WHERE conname='sessions_session_id_key';")" = "0" ] \
    || { echo "FATAL: global constraint still present"; exit 1; }
  [ "$(psqlq "SELECT count(*) FROM pg_proc WHERE proname='update_voucher_usage';")" = "1" ] \
    || { echo "FATAL: expected exactly one update_voucher_usage overload"; exit 1; }
fi

# ------------------------------------------------------------ 6. verify ------
say "6/6  Post-deploy verification"
if [ "$DRY_RUN" != "--dry-run" ]; then
  trap - ERR
  echo "  alembic head:  $(docker exec hotspot-backend alembic current 2>/dev/null | tail -1)"
  echo "  unique keys:   $(psqlq "SELECT string_agg(conname,', ') FROM pg_constraint WHERE conrelid='sessions'::regclass AND contype='u';")"
  echo "  usage fn:      $(psqlq "SELECT string_agg(oid::regprocedure::text,', ') FROM pg_proc WHERE proname='update_voucher_usage';")"
  echo "  last_interim:  $(psqlq "SELECT count(*) FROM information_schema.columns WHERE table_name='sessions' AND column_name='last_interim_at';") column(s)"
  echo
  echo "  Backup kept at: $BACKUP_DIR"
  echo "  WATCH NOW: docker compose logs -f freeradius   (confirm the next real login writes a sessions row)"
fi
say "Done"
