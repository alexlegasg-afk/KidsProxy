#!/usr/bin/env bash
# KidsProxy updater: backup db, ff-only pull, sync, test, restart, doctor.
# Refuses to run on a dirty tree. Never modifies .env or kidsproxy.db.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

die() { echo "ERROR: $*" >&2; exit 1; }
note() { echo "==> $*"; }

# --- 0. refuse dirty tree ---------------------------------------------------
if [[ -n "$(git status --porcelain)" ]]; then
  git status --short >&2
  die "working tree is dirty — commit or stash first (update.sh must not touch your changes)"
fi

# --- 1. timestamped db backup (read-only copy; db itself untouched) ---------
db="./kidsproxy.db"
if [[ -f .env ]]; then
  env_db="$(grep -E '^KP_DB_PATH=' .env | tail -1 | cut -d= -f2- || true)"
  [[ -n "$env_db" ]] && db="$env_db"
fi
ts="$(date +%Y%m%d-%H%M%S)"
mkdir -p backups
found_db=0
for f in "$db" "$db-wal" "$db-shm"; do
  if [[ -f "$f" ]]; then
    cp -p "$f" "backups/$(basename "$f")-$ts"
    note "backed up $f -> backups/$(basename "$f")-$ts"
    found_db=1
  fi
done
[[ "$found_db" -eq 1 ]] || note "no database file at $db yet (first run?) — nothing to back up"

# --- 2. update ---------------------------------------------------------------
note "git pull --ff-only"
git pull --ff-only

note "uv sync"
uv sync

note "running tests"
uv run --group dev pytest -q

# --- 3. restart --------------------------------------------------------------
if [[ "$(uname -s)" == "Darwin" ]] && launchctl print "gui/$(id -u)/com.kidsproxy" >/dev/null 2>&1; then
  launchctl kickstart -k "gui/$(id -u)/com.kidsproxy"
  note "launchd job restarted"
else
  echo "launchd job not running (or not macOS). Restart manually:"
  echo "  launchctl kickstart -k gui/\$(id -u)/com.kidsproxy    # macOS, agent installed by install.sh"
  echo "  # otherwise:"
  echo "  pkill -f 'uvicorn app.main:app'"
  echo "  uv run uvicorn app.main:app --host 127.0.0.1 --port 8787"
fi

# --- 4. smoke test -----------------------------------------------------------
note "Running doctor.sh (asks for a child key, hidden input)..."
./doctor.sh || note "doctor reported failures — see its PASS/FAIL output above."
note "Update complete."
