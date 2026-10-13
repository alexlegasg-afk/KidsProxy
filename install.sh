#!/usr/bin/env bash
# KidsProxy installer: deps, .env, database, children, optional launchd agent.
# Idempotent — safe to re-run. Never copies or moves .venv; never prints keys.
set -euo pipefail
umask 077

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

die() { echo "ERROR: $*" >&2; exit 1; }
note() { echo "==> $*"; }

# --- 1. prerequisite checks -------------------------------------------------
if [[ "$(uname -s)" == "Darwin" ]]; then
  command -v brew >/dev/null 2>&1 || die "Homebrew missing.
  Install it first:  /bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\""
  brew list python@3.12 >/dev/null 2>&1 \
    || note "Homebrew python@3.12 not found (uv will use its own managed Python instead; optionally: brew install python@3.12)"
fi
command -v uv >/dev/null 2>&1 || die "uv missing.
  Install it:  brew install uv     (or: curl -LsSf https://astral.sh/uv/install.sh | sh)"

# --- 2. dependencies: fresh .venv in place via uv sync ----------------------
note "Installing dependencies into .venv (uv sync)..."
uv sync

# --- 3. .env ----------------------------------------------------------------
write_env() {
  local base key models
  echo
  echo "KidsProxy needs three settings to reach your 9router upstream."
  read -r -p "Upstream base URL (default http://127.0.0.1:20128, no /v1 suffix): " base || true
  base="${base:-http://127.0.0.1:20128}"
  base="${base%/v1}"; base="${base%/}"        # tolerate a pasted /v1 or trailing slash
  read -r -s -p "Upstream API key (the 9router key; input hidden): " key || true; echo
  read -r -p "Model allowlist, comma-separated model IDs (default: the single model you enter now; empty = all models): " models || true
  key="${key:-}"; models="${models:-}"
  cat > .env <<EOF
# Written by install.sh $(date +%Y-%m-%d). Editable in the dashboard under Settings.
KP_DB_PATH=./kidsproxy.db
KP_UPSTREAM_BASE_URL=$base
KP_UPSTREAM_API_KEY=$key
KP_ALLOWED_MODELS=$models
EOF
  chmod 600 .env
  note ".env written (mode 600)."
}

if [[ -f .env ]]; then
  echo ".env already exists — keeping it."
  read -r -p "Overwrite with fresh values? [y/N] " ow || true
  [[ "$ow" =~ ^[Yy] ]] && write_env
else
  write_env
fi

# --- 4. database + admin password -------------------------------------------
note "Initialising database (schema + settings)..."
uv run python -m app.cli init-db
note "Set the parent-dashboard admin password (hidden prompt; re-running overwrites it)."
# Shell-side hidden read (not getpass): getpass's stdin fallback buffers and
# would swallow the remaining answers when input is piped.
admin_pw=""
read -r -s -p "admin password: " admin_pw || true; echo
if [[ -n "$admin_pw" ]]; then
  KP_ADMIN_PASSWORD="$admin_pw" uv run python -m app.cli set-admin-password
else
  note "no password entered — skipped (set later: uv run python -m app.cli set-admin-password)"
fi
unset admin_pw

# --- 5. children ------------------------------------------------------------
gen_host_ip() {
  local ip=""
  if [[ "$(uname -s)" == "Darwin" ]]; then
    ip="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || true)"
  fi
  [[ -z "$ip" ]] && ip="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
  [[ -z "$ip" ]] && ip="127.0.0.1"
  echo "$ip"
}

read -r -p "How many children to add? [0] " nkids || true
nkids="${nkids:-0}"
[[ "$nkids" =~ ^[0-9]+$ ]] || die "not a number: $nkids"

if [[ "$nkids" -gt 0 ]]; then
  mkdir -p generated
  ip="$(gen_host_ip)"
  for ((i = 1; i <= nkids; i++)); do
    echo
    read -r -p "Child #$i slug (lowercase, e.g. mia): " slug || true
    [[ -n "$slug" ]] || die "slug required"
    read -r -p "Child #$i display name (default $slug): " disp || true
    disp="${disp:-$slug}"
    read -r -p "Child #$i starting credit in USD (default 5): " credit || true
    credit="${credit:-5}"

    # Capture CLI output: the API key lands in the generated file only, never on screen.
    if ! out="$(uv run python -m app.cli add-child "$slug" --name "$disp" --credit "$credit" 2>&1)"; then
      if grep -q "already exists" <<<"$out"; then
        note "child '$slug' already exists — skipped (existing key is not re-shown; use 'uv run python -m app.cli rotate-key $slug' if needed)"
        continue
      fi
      printf '%s\n' "$out" >&2
      die "add-child failed for $slug"
    fi
    key="$(printf '%s\n' "$out" | sed -n 's/^API key (shown ONCE, store it now): //p')"
    [[ -n "$key" ]] || die "could not capture the key for $slug"

    cat > "generated/${slug}-models.yml" <<EOF
# KidsProxy provider for $disp ($slug).
# Merge into your OMP models.yml. KidsProxy enforces the allowlist and credits
# server-side; this file only points the client at the proxy.
# baseUrl uses this machine's LAN IP so a device on the same Wi-Fi can reach it.
# If the kid runs on this same machine, use http://127.0.0.1:8787/v1 instead.
providers:
  kidsproxy-$slug:
    baseUrl: "http://$ip:8787/v1"
    api: openai-completions
    apiKey: $key
    authHeaders: true
EOF
    chmod 600 "generated/${slug}-models.yml"
    note "Wrote generated/${slug}-models.yml (mode 600). Key is stored only in that file — it is never printed here."
  done
fi

# --- 6. optional launchd agent (macOS) --------------------------------------
if [[ "$(uname -s)" == "Darwin" ]]; then
  read -r -p "Install the launchd agent (start at login, keep running)? [y/N] " ld || true
  if [[ "$ld" =~ ^[Yy] ]]; then
    uv_bin="$(command -v uv)"
    plist="$HOME/Library/LaunchAgents/com.kidsproxy.plist"
    cat > "$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.kidsproxy</string>
  <key>ProgramArguments</key><array>
    <string>$uv_bin</string><string>run</string>
    <string>--directory</string><string>$REPO_ROOT</string>
    <string>uvicorn</string><string>app.main:app</string>
    <string>--host</string><string>127.0.0.1</string>
    <string>--port</string><string>8787</string>
  </array>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/tmp/kidsproxy.log</string>
  <key>StandardErrorPath</key><string>/tmp/kidsproxy.log</string>
</dict></plist>
EOF
    launchctl bootout "gui/$(id -u)/com.kidsproxy" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$plist"
    note "launchd agent installed: $plist (uv=$uv_bin, repo=$REPO_ROOT)"
  fi
else
  note "Non-macOS host: skipping launchd. Start manually:
  uv run uvicorn app.main:app --host 127.0.0.1 --port 8787"
fi

# --- 7. smoke test ----------------------------------------------------------
note "Running doctor.sh (will ask for a child key, hidden input)...)"
./doctor.sh || note "doctor reported failures — see its PASS/FAIL output above."
note "Install complete. Dashboard: http://127.0.0.1:8787/admin/"
