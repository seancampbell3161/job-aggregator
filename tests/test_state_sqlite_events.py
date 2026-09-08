# tests/test_state_sqlite_events.py
from src.sqlite_db import connect
from src.state_sqlite import SqlitePipelineEventsStore, _now_ms


def _store():
    return SqlitePipelineEventsStore(connect(":memory:"))


def test_record_and_recent_roundtrip():
    s = _store()
    s.record_cycle(tier="ats", fetched=10, matched=3, notified=1, duration_ms=1200)
    rows = s.recent_cycles(7)
    assert len(rows) == 1
    r = rows[0]
    assert (r["tier"], r["fetched"], r["matched"], r["notified"]) == ("ats", 10, 3, 1)
    assert r["ok"] is True
    assert r["failures"] == []


def test_failures_mark_cycle_not_ok():
    s = _store()
    s.record_cycle(
        tier="ats", fetched=5, matched=0, notified=0, duration_ms=900,
        failures=[{"source": "greenhouse:x", "error_type": "ReadTimeout"}],
    )
    r = s.recent_cycles(7)[0]
    assert r["ok"] is False
    assert r["failures"] == [{"source": "greenhouse:x", "error_type": "ReadTimeout"}]


def test_recent_window_excludes_old_rows():
    s = _store()
    old = _now_ms() - 10 * 86_400_000
    s._conn.execute(
        "INSERT INTO pipeline_events (ts_ms, tier, fetched, matched, notified, duration_ms, ok, failures) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (old, "ats", 1, 0, 0, 1, 1, "[]"),
    )
    s.record_cycle(tier="ats", fetched=2, matched=0, notified=0, duration_ms=1)
    assert len(s.recent_cycles(7)) == 1  # the 10-day-old row is outside the 7d window


def test_last_success_ignores_failed_cycles():
    s = _store()
    assert s.last_success_ms() is None
    s.record_cycle(
        tier="ats", fetched=1, matched=0, notified=0, duration_ms=1,
        failures=[{"source": "a", "error_type": "E"}],
    )
    assert s.last_success_ms() is None  # only failed cycles so far
    s.record_cycle(tier="ats", fetched=1, matched=0, notified=0, duration_ms=1)
    assert s.last_success_ms() is not None


def test_write_prunes_beyond_retention():
    s = _store()
    stale = _now_ms() - (s._RETENTION_DAYS + 1) * 86_400_000
    s._conn.execute(
        "INSERT INTO pipeline_events (ts_ms, tier, fetched, matched, notified, duration_ms, ok, failures) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (stale, "ats", 1, 0, 0, 1, 1, "[]"),
    )
    s.record_cycle(tier="ats", fetched=1, matched=0, notified=0, duration_ms=1)
    # the stale row is pruned on write; only the fresh one survives
    total = s._conn.execute("SELECT COUNT(*) AS c FROM pipeline_events").fetchone()["c"]
    assert total == 1


def test_record_persists_llm_failures():
    s = _store()
    s.record_cycle(
        tier="ats", fetched=10, matched=3, notified=1, duration_ms=1200,
        llm_failures=[{"stage": "relevance", "error_type": "ConnectError"}],
    )
    r = s.recent_cycles(7)[0]
    assert r["llm_failures"] == [{"stage": "relevance", "error_type": "ConnectError"}]


def test_llm_failures_default_empty_and_do_not_affect_ok():
    s = _store()
    # Fetch-ok cycle that nonetheless had LLM failures stays ok=1 (fetch-only).
    s.record_cycle(
        tier="ats", fetched=1, matched=1, notified=0, duration_ms=5,
        llm_failures=[{"stage": "gap", "error_type": "TimeoutError"}],
    )
    r = s.recent_cycles(7)[0]
    assert r["ok"] is True              # llm_failures must NOT flip ok
    assert s.last_success_ms() is not None   # ...and it still counts as a success
    # A cycle with no llm_failures arg defaults to []
    s.record_cycle(tier="ats", fetched=1, matched=0, notified=0, duration_ms=1)
    assert s.recent_cycles(7)[-1]["llm_failures"] == []


def test_migration_adds_llm_failures_column_to_legacy_db():
    """A pipeline_events table created without llm_failures gets the column on connect."""
    import sqlite3
    raw = sqlite3.connect(":memory:")
    raw.executescript(
        "CREATE TABLE pipeline_events ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, ts_ms INTEGER NOT NULL, tier TEXT NOT NULL,"
        " fetched INTEGER NOT NULL DEFAULT 0, matched INTEGER NOT NULL DEFAULT 0,"
        " notified INTEGER NOT NULL DEFAULT 0, duration_ms INTEGER NOT NULL DEFAULT 0,"
        " ok INTEGER NOT NULL DEFAULT 1, failures TEXT NOT NULL DEFAULT '[]');"
    )
    raw.execute(
        "INSERT INTO pipeline_events (ts_ms, tier) VALUES (?, ?)", (_now_ms(), "ats")
    )
    raw.commit()
    # The legacy DB lacks llm_failures; run the migration via _migrate (Step 3 adds it).
    from src.sqlite_db import _migrate
    _migrate(raw)
    cols = {row[1] for row in raw.execute("PRAGMA table_info(pipeline_events)")}
    assert "llm_failures" in cols
    # Pre-existing row backfilled to '[]'
    val = raw.execute("SELECT llm_failures FROM pipeline_events").fetchone()[0]
    assert val == "[]"


def test_migrate_is_idempotent():
    """Repeated _migrate (repeated connect()) must be a clean no-op, not a duplicate-column error."""
    import sqlite3
    from src.sqlite_db import _migrate
    raw = sqlite3.connect(":memory:")
    raw.executescript(
        "CREATE TABLE pipeline_events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts_ms INTEGER NOT NULL, "
        "tier TEXT NOT NULL, failures TEXT NOT NULL DEFAULT '[]');"
    )
    _migrate(raw)
    _migrate(raw)  # must not raise
    cols = {r[1] for r in raw.execute("PRAGMA table_info(pipeline_events)")}
    assert "llm_failures" in cols


class _LostRaceConn:
    """Duck-typed proxy: the first ALTER adds the column (simulating the concurrent
    winner) then raises, as our own losing ALTER would. All other calls pass through."""
    def __init__(self, real):
        self._real = real

    def execute(self, sql, *args, **kwargs):
        import sqlite3
        if sql.strip().upper().startswith("ALTER TABLE"):
            if "llm_failures" in sql:
                self._real.execute("ALTER TABLE pipeline_events ADD COLUMN llm_failures TEXT NOT NULL DEFAULT '[]'")
                raise sqlite3.OperationalError("duplicate column name: llm_failures")
            elif "new_count" in sql:
                self._real.execute("ALTER TABLE pipeline_events ADD COLUMN new_count INTEGER")
                raise sqlite3.OperationalError("duplicate column name: new_count")
        return self._real.execute(sql, *args, **kwargs)


def test_migrate_swallows_lost_race():
    """A duplicate-column OperationalError is swallowed when the column exists on re-read."""
    import sqlite3
    from src.sqlite_db import _migrate
    raw = sqlite3.connect(":memory:")
    raw.executescript(
        "CREATE TABLE pipeline_events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts_ms INTEGER NOT NULL, "
        "tier TEXT NOT NULL, failures TEXT NOT NULL DEFAULT '[]');"
    )
    _migrate(_LostRaceConn(raw))  # must NOT raise
    cols = {r[1] for r in raw.execute("PRAGMA table_info(pipeline_events)")}
    assert "llm_failures" in cols
    assert "new_count" in cols


class _GenuineErrorConn:
    """Duck-typed proxy whose ALTER raises WITHOUT adding the column — a real failure."""
    def __init__(self, real):
        self._real = real

    def execute(self, sql, *args, **kwargs):
        import sqlite3
        if sql.strip().upper().startswith("ALTER TABLE"):
            raise sqlite3.OperationalError("database is locked")
        return self._real.execute(sql, *args, **kwargs)


def test_migrate_reraises_genuine_error():
    """If the ALTER fails and the column is still absent, the error propagates."""
    import sqlite3
    import pytest
    from src.sqlite_db import _migrate
    raw = sqlite3.connect(":memory:")
    raw.executescript(
        "CREATE TABLE pipeline_events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts_ms INTEGER NOT NULL, "
        "tier TEXT NOT NULL, failures TEXT NOT NULL DEFAULT '[]');"
    )
    with pytest.raises(sqlite3.OperationalError):
        _migrate(_GenuineErrorConn(raw))


def test_record_cycle_persists_new_count():
    s = _store()
    s.record_cycle(tier="ats", fetched=10, matched=3, notified=1, duration_ms=100, new_count=7)
    assert s.recent_cycles(7)[0]["new_count"] == 7


def test_new_count_defaults_to_none():
    s = _store()
    s.record_cycle(tier="ats", fetched=10, matched=3, notified=1, duration_ms=100)
    assert s.recent_cycles(7)[0]["new_count"] is None


def test_last_cycle_ms_counts_failed_cycles_too():
    s = _store()
    assert s.last_cycle_ms() is None
    s.record_cycle(
        tier="ats", fetched=1, matched=0, notified=0, duration_ms=1,
        failures=[{"source": "a", "error_type": "E"}],
    )
    assert s.last_cycle_ms() is not None  # a failed cycle still proves liveness


def test_last_cycle_per_tier_returns_newest_row_per_tier():
    s = _store()
    old = _now_ms() - 3 * 86_400_000
    s._conn.execute(
        "INSERT INTO pipeline_events (ts_ms, tier, fetched, matched, notified, duration_ms, ok, failures, llm_failures) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (old, "ats", 1, 0, 0, 1, 1, "[]", "[]"),
    )
    s.record_cycle(tier="ats", fetched=2, matched=0, notified=0, duration_ms=1,
                   failures=[{"source": "x", "error_type": "E"}])
    s.record_cycle(tier="slow", fetched=3, matched=0, notified=0, duration_ms=1,
                   llm_failures=[{"stage": "relevance", "error_type": "ConnectError"}])
    rows = {r["tier"]: r for r in s.last_cycle_per_tier()}
    assert set(rows) == {"ats", "slow"}
    assert rows["ats"]["ts_ms"] > old            # newest ats row wins
    assert rows["ats"]["ok"] is False            # ok comes from that newest (failing) row
    assert rows["ats"]["degraded"] is False
    assert rows["slow"]["ok"] is True
    assert rows["slow"]["degraded"] is True      # llm_failures non-empty → degraded


def test_last_cycle_per_tier_empty_table():
    assert _store().last_cycle_per_tier() == []


def test_prune_preserves_newest_row_per_tier():
    """The retention prune must keep each tier's newest row forever — it is the
    anchor that keeps a long-dead tier's stall warning alive on /pipeline."""
    s = _store()
    ancient = _now_ms() - (s._RETENTION_DAYS + 10) * 86_400_000
    for ts, tier in ((ancient, "ats"), (ancient + 1000, "ats"), (ancient + 500, "digest")):
        s._conn.execute(
            "INSERT INTO pipeline_events (ts_ms, tier, fetched, matched, notified, duration_ms, ok, failures, llm_failures) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (ts, tier, 1, 0, 0, 1, 1, "[]", "[]"),
        )
    s.record_cycle(tier="ats", fetched=1, matched=0, notified=0, duration_ms=1)
    tiers = {r["tier"]: r for r in s.last_cycle_per_tier()}
    # digest's only (ancient) row survives as its per-tier anchor...
    assert tiers["digest"]["ts_ms"] == ancient + 500
    # ...but ats's ancient rows are pruned — its anchor is the fresh row.
    count_ats = s._conn.execute(
        "SELECT COUNT(*) AS c FROM pipeline_events WHERE tier='ats'"
    ).fetchone()["c"]
    assert count_ats == 1
