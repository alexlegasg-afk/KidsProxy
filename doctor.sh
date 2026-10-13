#!/usr/bin/env bash
# KidsProxy smoke test: PASS/FAIL per hop, with likely cause on failure.
# Reads config from .env; prints no secrets (API keys are never echoed).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

PROXY=http://127.0.0.1:8787
overall=0
ok()  { echo "PASS  $*"; }
bad() { echo "FAIL  $*"; overall=1; }
cause() { echo "      likely cause: $*"; }

[[ -f .env ]] || { echo "FAIL  .env missing — run ./install.sh first"; exit 1; }
# shellcheck source=/dev/null
set -a; . ./.env; set +a
# Tolerate a base URL pasted with /v1 or trailing slash.
UP="${KP_UPSTREAM_BASE_URL%/}"; UP="${UP%/v1}"

# JSON validation helper (no jq dependency)
if command -v python3 >/dev/null 2>&1; then PY=(python3); else PY=(uv run python); fi

tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT

echo "1) proxy listening on 8787"
pid=""
if command -v lsof >/dev/null 2>&1; then
  pid="$(lsof -nP -iTCP:8787 -sTCP:LISTEN -t 2>/dev/null | head -1 || true)"
elif command -v ss >/dev/null 2>&1; then
  pid="$(ss -ltnpH "( sport = :8787 )" 2>/dev/null | grep -oE 'pid=[0-9]+' | head -1 | cut -d= -f2 || true)"
fi
if [[ -n "$pid" ]]; then
  ok "listening (pid $pid)"
else
  bad "nothing listening on 8787"
  cause "proxy not started: launchd agent not loaded (launchctl bootstrap gui/\$(id -u) ~/Library/LaunchAgents/com.kidsproxy.plist) or it crashed — check /tmp/kidsproxy.log; or it is bound to another port"
fi

echo
echo "2) upstream $UP/v1/models with configured key"
up_headers=(-H "Authorization: Bearer ${KP_UPSTREAM_API_KEY:-none}")
[[ -z "${KP_UPSTREAM_API_KEY:-}" ]] && up_headers=(-H "Authorization: Bearer none")
code="$(curl -s -o "$tmp/up.json" -w '%{http_code}' --max-time 15 "${up_headers[@]}" "$UP/v1/models" || true)"
[[ -n "$code" ]] || code=000
case "$code" in
  200) ok "upstream responded 200" ;;
  401|403)
    bad "upstream responded $code"
    cause "KP_UPSTREAM_API_KEY in .env does not match 9router's master key" ;;
  000)
    bad "no connection to $UP"
    cause "wrong KP_UPSTREAM_BASE_URL in .env (must be 9router's root, no /v1 suffix) or 9router is down" ;;
  *)
    bad "upstream responded $code"
    cause "KP_UPSTREAM_BASE_URL points at the wrong server, or its auth setup differs" ;;
esac

echo
echo "3) requests through the proxy"
read -r -s -p "Child API key (sk-..., input hidden; press Enter to skip): " CK || true; echo
if [[ -z "$CK" ]]; then
  echo "SKIP  no child key provided — proxy hops not tested"
else
  # model list via proxy (also validates child-key auth end to end)
  model=""
  if curl -s --max-time 15 -o "$tmp/models.json" -w '%{http_code}' -H "Authorization: Bearer $CK" "$PROXY/v1/models" > "$tmp/mcode" 2>/dev/null; then
    if [[ "$(cat "$tmp/mcode")" == 200 ]]; then
      model="$("${PY[@]}" -c 'import json,sys; print(json.load(open(sys.argv[1]))["data"][0]["id"])' "$tmp/models.json" 2>/dev/null || true)"
    fi
  fi
  if [[ -z "$model" ]]; then
    bad "proxy /v1/models with child key failed (http $(cat "$tmp/mcode" 2>/dev/null || echo '?'))"
    case "$(cat "$tmp/mcode" 2>/dev/null || echo '')" in
      401) cause "child key wrong, rotated, revoked, or the child is paused" ;;
      402) cause "child has no credit — parent: uv run python -m app.cli credit <slug> <usd>" ;;
      000|"") cause "proxy down (see hop 1)" ;;
      *)    cause "proxy auth or config problem — check /tmp/kidsproxy.log" ;;
    esac
  else
    ok "proxy /v1/models ok (using model: $model)"

    # 3a. tiny non-streaming request (stream field omitted — regression for the SSE bug)
    scode="$(curl -s -o "$tmp/ns.json" -w '%{http_code}' --max-time 60 \
      -H "Authorization: Bearer $CK" -H 'Content-Type: application/json' \
      -d "{\"model\":\"$model\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}],\"max_tokens\":16}" \
      "$PROXY/v1/chat/completions" || true)"
    [[ -n "$scode" ]] || scode=000
    if [[ "$scode" == 200 ]] && "${PY[@]}" -c 'import json,sys; json.load(open(sys.argv[1]))' "$tmp/ns.json" 2>/dev/null; then
      ok "non-streaming request: 200, valid JSON"
    else
      bad "non-streaming request: http $scode"
      case "$scode" in
        401) cause "child key wrong/rotated/paused" ;;
        402) cause "no credit — app.cli credit <slug> <usd>" ;;
        502) cause "upstream rejected or returned SSE for a non-streaming call (stale KidsProxy? git pull + restart; or upstream auth/URL wrong — hops 1-2)" ;;
        000) cause "proxy down or timed out (hop 1, /tmp/kidsproxy.log)" ;;
        *)   cause "see /tmp/kidsproxy.log" ;;
      esac
    fi

    # 3b. tiny streaming request
    stcode="$(curl -s -N -o "$tmp/st.txt" -w '%{http_code}' --max-time 60 \
      -H "Authorization: Bearer $CK" -H 'Content-Type: application/json' \
      -d "{\"model\":\"$model\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}],\"max_tokens\":16,\"stream\":true}" \
      "$PROXY/v1/chat/completions" || true)"
    [[ -n "$stcode" ]] || stcode=000
    if [[ "$stcode" == 200 ]] && grep -q '^data: ' "$tmp/st.txt"; then
      ok "streaming request: 200, SSE data: chunks"
    else
      bad "streaming request: http $stcode$(grep -q '^data: ' "$tmp/st.txt" 2>/dev/null || echo ' (no SSE chunks)')"
      case "$stcode" in
        401) cause "child key wrong/rotated/paused" ;;
        402) cause "no credit" ;;
        000) cause "proxy down or upstream hung (hop 1-2, /tmp/kidsproxy.log)" ;;
        *)   cause "upstream streaming problem — see hop 2 and /tmp/kidsproxy.log" ;;
      esac
    fi
  fi
fi

echo
if [[ "$overall" -eq 0 ]]; then
  echo "doctor: ALL PASS"
else
  echo "doctor: FAILURES ABOVE"
fi
exit "$overall"
