---
name: verify
description: Build/launch/drive recipe for verifying job-aggregator changes end-to-end against the real FastAPI app with a scratch SQLite DB.
---

# Verifying job-aggregator changes

## Launch the web app (no Docker needed)

```bash
JOB_AGG_SQLITE_PATH=/path/to/scratch.db \
JOB_AGG_WEB_PORT=8901 \
.venv/bin/python -m src.web
```

- Use `.venv/bin/python` / `.venv/bin/pytest` (or `uv run …`) so the project
  venv is the interpreter that runs.
- `src.sqlite_db.connect(path)` creates the full schema on first open, so a
  fresh scratch path just works. A fresh DB has **no password and no
  settings**: every page redirects to `/welcome` until a password exists, then
  to `/login` without a session, then to `/setup` until you import settings.
- Server binds 127.0.0.1; ready within ~2s (poll with curl).
- The poller/scheduler is separate (`python -m src.scheduler`) — the web app
  alone renders everything from whatever is in the DB.

## Sign in

Everything except `/static/*`, `/login`, `/welcome`, `/logout`, `/tailor`, and
`/tailor/pdf` needs a session. Set the password with the CLI (same DB file),
then sign in with curl and reuse the cookie jar:

```bash
printf 'verify password 1\n' | JOB_AGG_SQLITE_PATH=/path/to/scratch.db .venv/bin/python -m src.settings set-password
curl -s -c /tmp/verify-jar -o /dev/null -w '%{http_code} %{redirect_url}\n' \
  --data-urlencode 'password=verify password 1' http://127.0.0.1:8901/login   # 303
curl -s -b /tmp/verify-jar http://127.0.0.1:8901/board                        # signed in
```

Or open `/welcome` in a browser on a fresh DB and create it there. curl posts
need no extra headers (no `Origin` header = not a browser); a browser's posts
must be same-origin, or they get `403 Cross-origin request blocked.`

## Seed settings

```bash
mkdir -p /tmp/verify-settings && cp config.example.yaml /tmp/verify-settings/config.yaml
JOB_AGG_SQLITE_PATH=/path/to/scratch.db .venv/bin/python -m src.settings import /tmp/verify-settings
```

Settings changes (import, `add-source`, `set-secret`) apply to the running app
on the next request — no restart. `python -m src.settings status` shows the
state.

## Seed telemetry / jobs

Insert rows directly with `src.sqlite_db.connect()` + SQL (see
`tests/test_state_sqlite_events.py` for the pipeline_events column list).
`record_cycle()` stamps "now" and prunes, so for controlled timestamps
(out-of-window tiers, old failures) use raw INSERTs with explicit `ts_ms`.

## Flows worth driving

- `GET /pipeline` — header strip (last cycle, last success), panels; `#cycles`
  div htmx-polls `GET /pipeline/cycles`.
- `GET /pipeline/cycles` — tier chips (ok/warn/bad+stalled), cycle-stats table,
  fetch/LLM failure tallies with recency + `dim`, recent-cycles `<details>`
  table, OOB spans `#last-cycle` / `#last-success`.
- Fail-soft probe: corrupt a row mid-flight
  (`sqlite3 db "UPDATE pipeline_events SET llm_failures='{not json' ..."`) —
  the page must stay HTTP 200 with the panel degraded to "unavailable".
- Cold-start probe: point at a brand-new empty DB — expect "No cycles in
  window" / "never", no recent-cycles section, HTTP 200.

## Gotchas

- SQLite runs in rollback-journal mode — external `sqlite3` writes are visible
  to the running app immediately; no restart needed.
