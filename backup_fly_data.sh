#!/usr/bin/env bash
#
# Pull the Fly volume's data files into a dated snapshot under ./backups/.
#
# Why this exists: /data/history.csv on the Fly volume is the canonical record, and the
# only other copy is the 30-day rolling Gist mirror (HISTORY_MIRROR_DAYS). Losing that
# volume would destroy every row older than 30 days — including the pre-insulation
# baseline the whole building-performance question depends on. The repo lives in
# ProtonDrive CloudStorage, so a file landing here is cloud-synced for free; that is the
# entire durability story, no second service required.
#
# Safety rule that shapes the whole script: a failed or partial pull must NEVER replace a
# good snapshot. Everything is staged to a temp dir and validated before anything is moved
# into backups/. A backup that silently truncates is worse than no backup at all.
#
# Usage:  ./backup_fly_data.sh
# Cron:   0 9 * * 1  cd /path/to/WindowClose && ./backup_fly_data.sh >> backups/cron.log 2>&1
set -euo pipefail

# cron does not inherit an interactive shell's PATH, and a missing `fly` is the usual
# reason a scheduled backup quietly stops running.
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

cd "$(dirname "$0")"

readonly APP="window-watch-ollie"
readonly BACKUP_ROOT="backups"
readonly TODAY="$(date +%Y-%m-%d)"
readonly DEST="${BACKUP_ROOT}/${TODAY}"
readonly STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

# Must match HISTORY_HEADER in window_watch.py. If the schema is widened there, this
# check fires and the backup refuses — deliberately. A header we don't recognise means
# either a schema change we should notice, or a corrupt pull.
readonly EXPECTED_HEADER='timestamp,outdoor_c,feels_like_c,outdoor_humidity_pct,wind_kmh,gusts_kmh,solar_wm2,cloud_pct,precip_mm,indoor_c,indoor_humidity_pct,battery_pct,status,window_actual,blinds_actual,zones,outdoor_source'

# history.csv is the irreplaceable one; the rest are cheap to lose (calibration is
# re-derivable from history, the reports and state regenerate within a run or two) so a
# missing one warns rather than aborts.
readonly CRITICAL="history.csv"
readonly OPTIONAL=("calibration.json" "state.json" "window_report.json" "blind_report.json")

fail() { echo "[FAIL] $*" >&2; write_last_run "FAILED: $*" "-"; exit 1; }
warn() { echo "[warn] $*" >&2; }

write_last_run() {
    mkdir -p "$BACKUP_ROOT"
    {
        echo "last_attempt: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
        echo "status:       $1"
        echo "history_rows: $2"
        echo "snapshot:     ${DEST}"
    } > "${BACKUP_ROOT}/LAST_RUN"
}

echo "==> Pulling /data from ${APP}"

if ! fly ssh sftp get "/data/${CRITICAL}" "${STAGE}/${CRITICAL}" -a "$APP" -q; then
    fail "could not fetch /data/${CRITICAL} (fly auth expired? machine stopped?)"
fi

for f in "${OPTIONAL[@]}"; do
    if ! fly ssh sftp get "/data/${f}" "${STAGE}/${f}" -a "$APP" -q 2>/dev/null; then
        warn "optional file /data/${f} not fetched — continuing"
    fi
done

# ---- Validate before anything is allowed near backups/ --------------------------------
echo "==> Validating"

[ -s "${STAGE}/${CRITICAL}" ] || fail "${CRITICAL} is empty or missing after fetch"

header="$(head -n 1 "${STAGE}/${CRITICAL}")"
[ "$header" = "$EXPECTED_HEADER" ] \
    || fail "${CRITICAL} header does not match window_watch.py's HISTORY_HEADER — schema change or corrupt pull"

rows="$(($(wc -l < "${STAGE}/${CRITICAL}") - 1))"
[ "$rows" -gt 0 ] || fail "${CRITICAL} has a header but no data rows"

# The last row's timestamp must parse, or we fetched something malformed.
last_ts="$(tail -n 1 "${STAGE}/${CRITICAL}" | cut -d, -f1)"
date -j -f "%Y-%m-%dT%H:%M:%SZ" "$last_ts" >/dev/null 2>&1 \
    || fail "last row timestamp does not parse: '${last_ts}'"

# Monotonicity: history is append-only, so a snapshot with fewer rows than the previous
# one means truncation upstream. Refuse rather than record the loss.
prev="$(find "$BACKUP_ROOT" -mindepth 2 -maxdepth 2 -name "$CRITICAL" 2>/dev/null \
        | grep -v "/${TODAY}/" | sort | tail -n 1 || true)"
if [ -n "$prev" ]; then
    prev_rows="$(($(wc -l < "$prev") - 1))"
    if [ "$rows" -lt "$prev_rows" ]; then
        fail "row count went BACKWARDS: ${rows} now vs ${prev_rows} in ${prev} — refusing to overwrite"
    fi
    echo "    ${rows} rows (previous snapshot: ${prev_rows}, +$((rows - prev_rows)))"
else
    echo "    ${rows} rows (first snapshot)"
fi
echo "    latest row: ${last_ts}"

# ---- Commit the snapshot --------------------------------------------------------------
mkdir -p "$DEST"
mv "${STAGE}"/* "$DEST"/
write_last_run "OK" "$rows"

echo "==> Snapshot written to ${DEST}"
ls -la "$DEST"
