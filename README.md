# KidsProxy

Prepaid AI gateway for kids. Sits in front of [9router](https://github.com/lostfictions/9router) (or any OpenAI-compatible server) and gives each child:

- their own API key (`sk-<slug>-...`, shown once, stored only as a hash)
- a prepaid balance in integer microdollars (1 USD = 1 000 000)
- reservation-based billing: funds reserved before the request, settled from real token usage after
- time-of-day / daily / monthly spend caps, model allowlist, concurrency cap
- a parent dashboard: charts, credit buttons, pause child, rotate/revoke keys

Child keys never reach the upstream. The master upstream key stays server-side only.

## Requirements

- macOS or Linux, Python 3.12+ — easiest via [uv](https://docs.astral.sh/uv/): `brew install uv`
- 9router (or any OpenAI-compatible endpoint) reachable from this machine

## Get the code onto the MacBook Air

The `.venv` is machine-specific — never copy it. Rebuild deps on the Air.

1. Easiest: on the Air, download the ZIP from GitHub (`Code` → `Download ZIP`), or clone:

   ```sh
   git clone https://github.com/alexlegasg-afk/KidsProxy.git
   ```

   Or zip source locally and AirDrop it:

   ```sh
   cd ~/projects/KidsProxy
   zip -r ~/Desktop/kidsproxy.zip . -x ".venv/*" "*.db*" ".env" "__pycache__/*" ".pytest_cache/*"
   ```

2. On the Air, unpack and install:

   ```sh
   mkdir -p ~/projects && cd ~/projects
   unzip ~/Downloads/KidsProxy-main.zip -d KidsProxy   # GitHub ZIP: KidsProxy-main.zip
   cd KidsProxy
   uv sync            # creates fresh .venv, installs all deps
   ```

3. If you want the existing balances/history: copy `kidsproxy.db` (all `.db*` files, server stopped) into the project dir. Otherwise skip — `init-db` makes a clean one.

## First run (on the Air)

```sh
cd ~/projects/KidsProxy
cp .env.example .env
```

Edit `.env`:

| var | what to set |
|---|---|
| `KP_UPSTREAM_BASE_URL` | where 9router listens on the Air: usually `http://127.0.0.1:20128` |
| `KP_UPSTREAM_API_KEY` | 9router's master key (empty if 9router needs none) |
| `KP_TZ` | your timezone, e.g. `America/Los_Angeles` |

Then:

```sh
uv run python -m app.cli init-db                 # schema + seed settings
uv run python -m app.cli set-admin-password      # parent dashboard login
uv run python -m app.cli add-child mia --credit 5   # prints key ONCE — save it
uv run uvicorn app.main:app --host 127.0.0.1 --port 8787
```

Dashboard: `http://127.0.0.1:8787/admin/` (login = the password you set).
Prices, hours windows, daily/monthly caps, model allowlist — all editable in the dashboard under Settings; no restart needed.

Smoke test it (replace the key with the one `add-child` printed):

```sh
curl http://127.0.0.1:8787/v1/models -H "Authorization: Bearer sk-mia-..."
curl http://127.0.0.1:8787/v1/chat/completions \
  -H "Authorization: Bearer sk-mia-..." -H "Content-Type: application/json" \
  -d '{"model":"<a model from /v1/models>","messages":[{"role":"user","content":"hi"}]}'
```

## Wire OMP on the Air to KidsProxy

OMP talks OpenAI-compatible HTTP, so it just needs base URL + the child's key:

- base URL: `http://127.0.0.1:8787/v1`
- API key: the child key `sk-mia-...` (never 9router's master key)
- model: whatever `/v1/models` lists (KidsProxy filters it by the child's allowlist)

In OMP config (`~/.omp/config.yml` or per your setup), add/point a provider:

```yaml
providers:
  kidsproxy:
    type: openai-compatible
    baseURL: http://127.0.0.1:8787/v1
    apiKey: sk-mia-...
```

Every request OMP makes now spends from mia's prepaid balance and obeys her caps. Streaming works (KidsProxy passes SSE through and bills from the final usage chunk).

## CLI reference

```sh
uv run python -m app.cli status                      # balances per child
uv run python -m app.cli credit mia 2.50             # +$2.50
uv run python -m app.cli rotate-key mia              # new key, shown once
uv run python -m app.cli release-uncertain [slug]    # free funds held by unrecoverable requests
```

## How billing stays honest

- Before forwarding: estimate prompt tokens (chars/3), reserve worst-case cost (`max_tokens` × output price). Balance below `KP_MIN_START_BALANCE` → 402, request never hits upstream.
- After: settle from real `usage` (cached-input priced separately). Over-reservation refunded; under-charge billed to the cap `KP_MAX_REQUEST_COST`.
- Crash / truncated stream / missing usage → request marked `uncertain`, funds held (`KP_UNCERTAIN_POLICY=hold`) until a parent runs `release-uncertain` or flips policy to `release`. Never silently billed zero, never silently kept.
- Every cent movement is a ledger row; dashboard shows everything.

## Run at login (macOS)

`launchctl` keeps it up without a terminal:

```sh
cat > ~/Library/LaunchAgents/com.kidsproxy.plist <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.kidsproxy</string>
  <key>ProgramArguments</key><array>
    <string>/opt/homebrew/bin/uv</string><string>run</string>
    <string>--directory</string><string>$HOME/projects/KidsProxy</string>
    <string>uvicorn</string><string>app.main:app</string>
    <string>--host</string><string>127.0.0.1</string><string>--port</string><string>8787</string>
  </array>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/tmp/kidsproxy.log</string>
  <key>StandardErrorPath</key><string>/tmp/kidsproxy.log</string>
</dict></plist>
PLIST
launchctl load ~/Library/LaunchAgents/com.kidsproxy.plist
```

## Tests

```sh
uv run --group dev pytest -q    # 16 tests: billing math, races, caps, uncertain flow, dashboard
```

## Install & update (one command each)

```sh
./install.sh    # one-time setup: deps, .env (asks 3 questions), database, admin password,
                # children + a ready-to-copy models.yml per child in ./generated/ (600, gitignored),
                # optional launchd agent; ends with ./doctor.sh
./update.sh     # refuses a dirty tree; backs up kidsproxy.db*, git pull --ff-only, uv sync,
                # runs tests, restarts the launchd job (or prints manual steps), then ./doctor.sh
./doctor.sh     # smoke test: PASS/FAIL per hop (proxy up, upstream auth, non-streaming +
                # streaming request through the proxy) with the likely cause for each failure
```

All three are idempotent and shellcheck-clean; none of them prints API keys (child keys land only in `./generated/<slug>-models.yml`, mode 600). `update.sh` never touches `.env` or `kidsproxy.db` (it copies the db to `backups/` with a timestamp first).

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `./doctor.sh` hop 1 FAIL: nothing on 8787 | launchd agent not loaded / server crashed | `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.kidsproxy.plist`; check `/tmp/kidsproxy.log` |
| Hop 2 FAIL: 401 from upstream | `KP_UPSTREAM_API_KEY` ≠ 9router master key | fix `.env`, restart |
| Hop 2 FAIL: 000 / connection refused | `KP_UPSTREAM_BASE_URL` wrong or 9router down | base URL = 9router root, **no** `/v1` suffix |
| Hop 3 FAIL: 401 | child key rotated/revoked, or child paused | dashboard → rotate key again, or `uv run python -m app.cli rotate-key <slug>` |
| Hop 3 FAIL: 402 | child balance below minimum | dashboard credit button or `uv run python -m app.cli credit <slug> 5` |
| Hop 3 FAIL: 502 on non-streaming call | stale KidsProxy before the SSE hotfix | `./update.sh` (pull + restart) |
| `./update.sh` refuses to run | dirty working tree | commit or stash, re-run |

## Security notes

- Child keys: SHA-256 hash at rest; plaintext shown once, unrecoverable.
- Dashboard: session cookie signed (`KP_SESSION_SECRET`, HttpOnly, SameSite=Lax), CSRF token on every mutating form, login throttled 8/5min with 0.5s penalty.
- `.env` and `*.db` are gitignored. Never commit them.
