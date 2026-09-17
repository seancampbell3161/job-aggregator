"""Shared SQLite helpers for tests (the test suite's only store backend)."""
from __future__ import annotations

import json
import sqlite3

from src.auth.store import SqliteAuthStore
from src.settings.store import SqliteSettingsStore
from src.state_sqlite import (
    SqliteBuilderSettingsStore,
    SqliteCoachRunsStore,
    SqliteConnectorHealthStore,
    SqliteDiscoveredBoardsStore,
    SqliteDiscoveredSlugsStore,
    SqliteOpsAlertStateStore,
    SqlitePipelineEventsStore,
    SqliteRejectedPostingsStore,
    SqliteSeenJobsStore,
    SqliteSourceStateStore,
)
from src.stores import Stores


def raw_seen_item(store: SqliteSeenJobsStore, job_id: str) -> dict:
    """The full stored item dict for one seen_jobs row (TTL-expired rows
    included) — a raw fetch, bypassing the store's normal read helpers."""
    row = store._conn.execute(
        "SELECT data FROM seen_jobs WHERE job_id = ?", (job_id,)
    ).fetchone()
    assert row is not None, f"no seen_jobs row for {job_id!r}"
    return json.loads(row["data"])


def raw_seen_items(store: SqliteSeenJobsStore) -> list[dict]:
    """Every stored seen_jobs item, ordered by job_id — a raw table scan."""
    return [
        json.loads(r["data"])
        for r in store._conn.execute("SELECT data FROM seen_jobs ORDER BY job_id")
    ]


def sqlite_stores(conn: sqlite3.Connection) -> Stores:
    """Every store wired over one connection, including settings — unlike
    build_stores(), which gives settings its own (see its docstring); tests
    share one in-memory connection because ":memory:" can't be reopened."""
    return Stores(
        seen=SqliteSeenJobsStore(conn),
        source_state=SqliteSourceStateStore(conn),
        discovered=SqliteDiscoveredSlugsStore(conn),
        health=SqliteConnectorHealthStore(conn),
        events=SqlitePipelineEventsStore(conn),
        rejected=SqliteRejectedPostingsStore(conn),
        alert_state=SqliteOpsAlertStateStore(conn),
        boards=SqliteDiscoveredBoardsStore(conn),
        coach=SqliteCoachRunsStore(conn),
        builder=SqliteBuilderSettingsStore(conn),
        settings=SqliteSettingsStore(conn),
        auth=SqliteAuthStore(conn),
    )
