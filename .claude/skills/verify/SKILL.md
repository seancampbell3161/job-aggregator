---
name: verify
description: Build/launch/drive recipe for verifying job-aggregator changes end-to-end against the real FastAPI app with a scratch SQLite DB.
---

# Verifying job-aggregator changes

## Launch the web app (no Docker needed)

```bash
JOB_AGG_BACKEND=sqlite \
JOB_AGG_SQLITE_PATH=/path/to/scratch.db \
JOB_AGG_WEB_PORT=8901 \
.venv/bin/python -m src.web
```

- Use `.venv/bin/python` / `.venv/bin/pytest` (or `uv run …`) so the project
  venv is the interpreter that runs.
- `src.sqlite_db.connect(path)` creates the full schema on first open, so a
  fresh scratch path just works; `config.yaml` is read fail-soft (no secrets
  needed for the web UI).
- Server binds 127.0.0.1; ready within ~2s (poll with curl).
- The poller/scheduler is separate (`python -m src.scheduler`) — the web app
  alone renders everything from whatever is in the DB.

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

- SQLite is WAL — external `sqlite3` writes are visible to the running app
  immediately; no restart needed.
