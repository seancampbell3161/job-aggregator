# tests/test_sqlite_db.py
import src.sqlite_db as sqlite_db
from src.sqlite_db import connect, integrity_check_and_repair


def test_connect_creates_all_tables(tmp_path):
    conn = connect(str(tmp_path / "t.db"))
    names = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    assert {"seen_jobs", "source_state", "discovered_slugs", "connector_health"} <= names


def test_connect_uses_rollback_journal_not_wal(tmp_path):
    # Rollback-journal (not WAL): WAL's -shm index doesn't coordinate across the
    # poller/web containers over the macOS bind mount, so a long-lived reader saw
    # stale data. See connect() docstring.
    conn = connect(str(tmp_path / "t.db"))
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "delete"


def test_connect_memory_ok():
    conn = connect(":memory:")
    assert conn.execute("SELECT 1").fetchone()[0] == 1
    names = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    assert {"seen_jobs", "source_state", "discovered_slugs", "connector_health"} <= names


def test_connect_uses_env_path(monkeypatch, tmp_path):
    db_path = str(tmp_path / "env_test.db")
    monkeypatch.setenv("JOB_AGG_SQLITE_PATH", db_path)
    conn = connect()
    assert (tmp_path / "env_test.db").exists()
    names = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    assert {"seen_jobs", "source_state", "discovered_slugs", "connector_health"} <= names


def test_schema_has_rejected_postings_table():
    from src.sqlite_db import connect
    conn = connect(":memory:")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(rejected_postings)")}
    assert {"job_id", "first_seen", "rejected_by", "title", "company",
            "location_text", "source", "apply_url", "posted_at",
            "comp_min", "comp_max", "verdict", "verdict_at", "data"} <= cols


def test_schema_has_ops_alert_state_table():
    from src.sqlite_db import connect
    conn = connect(":memory:")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(ops_alert_state)")}
    assert {"condition", "active", "last_sent_ms"} <= cols


def test_migrate_adds_new_count_to_existing_pipeline_events(tmp_path):
    """A DB created before new_count existed gains the column on connect();
    pre-migration rows read back as NULL (the evaluator must skip them)."""
    import sqlite3
    from src.sqlite_db import connect
    path = str(tmp_path / "old.db")
    old = sqlite3.connect(path)
    old.execute(
        "CREATE TABLE pipeline_events ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, ts_ms INTEGER NOT NULL, "
        "tier TEXT NOT NULL, fetched INTEGER NOT NULL DEFAULT 0, "
        "matched INTEGER NOT NULL DEFAULT 0, notified INTEGER NOT NULL DEFAULT 0, "
        "duration_ms INTEGER NOT NULL DEFAULT 0, ok INTEGER NOT NULL DEFAULT 1, "
        "failures TEXT NOT NULL DEFAULT '[]', llm_failures TEXT NOT NULL DEFAULT '[]')"
    )
    old.execute(
        "INSERT INTO pipeline_events (ts_ms, tier) VALUES (1, 'ats')"
    )
    old.commit()
    old.close()
    conn = connect(path)
    row = conn.execute("SELECT new_count FROM pipeline_events").fetchone()
    assert row["new_count"] is None


def test_integrity_check_ok_on_clean_db():
    """A healthy DB reports ok and triggers no repair."""
    conn = connect(":memory:")
    res = integrity_check_and_repair(conn)
    assert res.ok is True
    assert res.repaired is False
    assert res.messages == ["ok"]


def test_integrity_check_reindexes_and_recovers(monkeypatch):
    """Corruption detected → REINDEX runs → re-check clean → ok + repaired."""
    conn = connect(":memory:")
    checks = iter([["row 695 missing from index sqlite_autoindex_source_state_1"], ["ok"]])
    reindexed = {"called": 0}
    monkeypatch.setattr(sqlite_db, "_integrity_messages", lambda c: next(checks))
    monkeypatch.setattr(sqlite_db, "_reindex", lambda c: reindexed.__setitem__("called", 1))
    res = integrity_check_and_repair(conn)
    assert reindexed["called"] == 1
    assert res.ok is True
    assert res.repaired is True


def test_integrity_check_unrecoverable(monkeypatch):
    """REINDEX runs but corruption persists → not ok, repaired attempted."""
    conn = connect(":memory:")
    monkeypatch.setattr(sqlite_db, "_integrity_messages", lambda c: ["still corrupt"])
    monkeypatch.setattr(sqlite_db, "_reindex", lambda c: None)
    res = integrity_check_and_repair(conn)
    assert res.ok is False
    assert res.repaired is True
    assert res.messages == ["still corrupt"]
