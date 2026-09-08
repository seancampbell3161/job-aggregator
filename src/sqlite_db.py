# src/sqlite_db.py
from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_jobs (
    job_id      TEXT PRIMARY KEY,
    first_seen  TEXT,
    notified    INTEGER,
    ttl         INTEGER,
    score       INTEGER,
    title       TEXT,
    data        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS source_state (
    connector_name TEXT PRIMARY KEY,
    data           TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS discovered_slugs (
    connector_name TEXT PRIMARY KEY,
    data           TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS discovered_boards (
    domain TEXT PRIMARY KEY,
    data   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS connector_health (
    connector_name TEXT PRIMARY KEY,
    dead_streak    INTEGER DEFAULT 0,
    suppressed     INTEGER DEFAULT 0,
    updated_at     TEXT,
    backoff_until  INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS pipeline_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms       INTEGER NOT NULL,
    tier        TEXT NOT NULL,
    fetched     INTEGER NOT NULL DEFAULT 0,
    matched     INTEGER NOT NULL DEFAULT 0,
    notified    INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    ok          INTEGER NOT NULL DEFAULT 1,
    failures    TEXT NOT NULL DEFAULT '[]',
    llm_failures TEXT NOT NULL DEFAULT '[]',
    new_count   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_pipeline_events_ts ON pipeline_events (ts_ms);
CREATE TABLE IF NOT EXISTS rejected_postings (
    job_id        TEXT PRIMARY KEY,
    first_seen    TEXT NOT NULL,
    rejected_by   TEXT NOT NULL,
    title         TEXT,
    company       TEXT,
    location_text TEXT,
    source        TEXT,
    apply_url     TEXT,
    posted_at     TEXT,
    comp_min      INTEGER,
    comp_max      INTEGER,
    verdict       TEXT,
    verdict_at    TEXT,
    data          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rejected_first_seen ON rejected_postings (first_seen);
CREATE TABLE IF NOT EXISTS coach_runs (
    run_id     TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    provider   TEXT,
    model      TEXT,
    status     TEXT NOT NULL,
    snapshot   TEXT,
    result     TEXT,
    error      TEXT
);
CREATE INDEX IF NOT EXISTS idx_coach_runs_created ON coach_runs (created_at);
CREATE TABLE IF NOT EXISTS ops_alert_state (
    condition    TEXT PRIMARY KEY,
    active       INTEGER NOT NULL DEFAULT 0,
    last_sent_ms INTEGER
);
CREATE TABLE IF NOT EXISTS builder_settings (
    id   INTEGER PRIMARY KEY CHECK (id = 1),
    data TEXT NOT NULL
);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """Idempotent, framework-free column adds for DBs created before a column
    existed. The CREATE TABLE statements are IF NOT EXISTS, so new columns on an
    existing table must be added here. Safe to run on every connect(), and safe
    under concurrent connect() from the poller and web processes that share one
    bind-mounted DB file."""
    _add_column_if_missing(
        conn, "pipeline_events", "llm_failures",
        "ALTER TABLE pipeline_events ADD COLUMN llm_failures TEXT NOT NULL DEFAULT '[]'",
    )
    _add_column_if_missing(
        # NULLable, no default: pre-migration rows must stay distinguishable
        # from real zeros (the zero-yield alert evaluator ignores NULL rows).
        conn, "pipeline_events", "new_count",
        "ALTER TABLE pipeline_events ADD COLUMN new_count INTEGER",
    )
    _add_column_if_missing(
        conn, "connector_health", "backoff_until",
        "ALTER TABLE connector_health ADD COLUMN backoff_until INTEGER NOT NULL DEFAULT 0",
    )


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    """Idempotent ADD COLUMN, race-safe under the poller+web shared bind-mount DB:
    a concurrent connect() may add the column between our PRAGMA read and the
    ALTER, so on OperationalError we re-read and only propagate if it's still
    absent (a real failure)."""
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if not cols:
        return  # table doesn't exist yet — _SCHEMA's CREATE TABLE includes the column
    if column in cols:
        return
    try:
        conn.execute(ddl)
    except sqlite3.OperationalError:
        cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            raise


@dataclass(frozen=True)
class IntegrityResult:
    ok: bool                  # database is sound (now)
    repaired: bool            # a REINDEX was run to get there
    messages: list[str]       # final PRAGMA integrity_check output


def _integrity_messages(conn: sqlite3.Connection) -> list[str]:
    """Full integrity_check (not quick_check — quick_check skips the index-vs-table
    verification that catches the 'row N missing from index' corruption that has
    silently killed the ats tier here)."""
    return [r[0] for r in conn.execute("PRAGMA integrity_check")]


def _reindex(conn: sqlite3.Connection) -> None:
    """Rebuild every index from the (intact) table data. Repairs a corrupt index
    with no data loss; cheap on this small DB."""
    conn.execute("REINDEX")


def integrity_check_and_repair(conn: sqlite3.Connection) -> IntegrityResult:
    """Detect SQLite corruption and self-heal the common case (a corrupt index)
    with REINDEX. Intended to run periodically on the poller process: because the
    poller both writes the fix and reads the DB on the same connection/process, it
    sees the repaired pages immediately — unlike a repair issued from the web
    container, which the poller couldn't observe until a restart (the macOS
    bind-mount page-cache incoherence noted in connect()'s docstring).

    Loud by design: a corrupt result is logged at ERROR even when self-healed, so
    a recurring repair is visible rather than silent. Never raises — a maintenance
    pass must not crash the scheduler."""
    try:
        before = _integrity_messages(conn)
    except Exception as exc:  # noqa: BLE001 — maintenance is best-effort
        log.error("db_integrity_check_failed", extra={"error": str(exc)})
        return IntegrityResult(ok=False, repaired=False, messages=[str(exc)])
    if before == ["ok"]:
        log.debug("db_integrity_ok")
        return IntegrityResult(ok=True, repaired=False, messages=before)

    log.error("db_integrity_corrupt", extra={"messages": before[:10]})
    try:
        _reindex(conn)
    except Exception as exc:  # noqa: BLE001 — repair attempt is best-effort
        log.error("db_reindex_failed", extra={"error": str(exc), "messages": before[:10]})
        return IntegrityResult(ok=False, repaired=False, messages=before)

    after = _integrity_messages(conn)
    if after == ["ok"]:
        # ERROR (not INFO): corruption occurred and was auto-repaired — surface it
        # so a recurring root cause (e.g. bind-mount torn writes) gets attention.
        log.error("db_reindex_recovered", extra={"before": before[:10]})
        return IntegrityResult(ok=True, repaired=True, messages=after)
    log.error("db_integrity_unrecoverable", extra={"messages": after[:10]})
    return IntegrityResult(ok=False, repaired=True, messages=after)


def connect(path: str | None = None) -> sqlite3.Connection:
    """Open (creating if needed) the local SQLite DB in rollback-journal mode
    with a busy timeout, so the poller and web processes can share one file.

    NOT WAL: in the docker-compose stack the poller and web run in separate
    containers sharing this file over a macOS bind mount. WAL coordinates
    readers and writers through a shared-memory index (the `-shm` file), and
    that mmap does not coordinate across containers on the bind mount — so a
    long-lived reader (the web app's startup connection) served a stale snapshot
    and never saw the poller's writes until it reconnected. Rollback-journal
    mode has no `-shm`: every transaction reads the main db file directly, so
    all committed writes are visible immediately in either direction. The cost
    is brief reader/writer lock contention, which the tiny write volume (poller
    a few times/min, rare user writes) makes negligible — busy_timeout covers it.

    isolation_level=None gives autocommit; transactions are issued explicitly
    with BEGIN IMMEDIATE where atomicity matters."""
    path = path or os.environ.get("JOB_AGG_SQLITE_PATH", "data/job_aggregator.db")
    if path != ":memory:":
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # Switching an existing WAL database here checkpoints and folds the `-wal`
    # back into the main file, then removes the `-wal`/`-shm` sidecars.
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("PRAGMA busy_timeout=5000")
    for stmt in _SCHEMA.strip().split(";"):
        if stmt.strip():
            conn.execute(stmt)
    _migrate(conn)
    return conn
