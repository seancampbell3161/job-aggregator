# tests/test_coach_store.py
"""SqliteCoachRunsStore round-trips. SQLite-only, like the rejected store."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from src.sqlite_db import connect
from src.state_sqlite import SqliteCoachRunsStore

_CARDS = [{"category": "filters", "title": "Widen titles",
           "evidence": "0 of 12 staff", "action": "add staff", "impact": "high"}]
_SNAP = {"meta": {"applied_count": 3, "low_sample": True}}


@pytest.fixture
def store():
    return SqliteCoachRunsStore(connect(":memory:"))


def _record(store, run_id, created_at, status="ok"):
    store.record(
        run_id=run_id, created_at=created_at, provider="ollama", model="gpt-oss:120b",
        status=status, snapshot_json=json.dumps(_SNAP), result_json=json.dumps(_CARDS),
        error=None if status == "ok" else "ConnectError",
    )


def test_record_get_round_trip(store):
    _record(store, "r1", "2026-07-12T10:00:00+00:00")
    run = store.get("r1")
    assert run["run_id"] == "r1"
    assert run["status"] == "ok"
    assert run["provider"] == "ollama"
    assert run["cards"] == _CARDS
    assert run["snapshot"]["meta"]["low_sample"] is True
    assert store.get("missing") is None


def test_latest_returns_newest(store):
    assert store.latest() is None
    _record(store, "r1", "2026-07-10T10:00:00+00:00")
    _record(store, "r2", "2026-07-12T10:00:00+00:00")
    assert store.latest()["run_id"] == "r2"


def test_list_runs_light_rows_newest_first_limited(store):
    for i in range(5):
        _record(store, f"r{i}", f"2026-07-{10 + i:02d}T10:00:00+00:00")
    runs = store.list_runs(limit=3)
    assert [r["run_id"] for r in runs] == ["r4", "r3", "r2"]
    assert "cards" not in runs[0] and "snapshot" not in runs[0]


def test_error_run_round_trip(store):
    store.record(run_id="bad", created_at="2026-07-12T10:00:00+00:00",
                 status="error", error="ConnectError")
    run = store.get("bad")
    assert run["status"] == "error" and run["error"] == "ConnectError"
    assert run["cards"] == [] and run["snapshot"] is None


def test_prune_older_than(store):
    # The surviving row is dated relative to now, and the doomed one is ancient.
    # prune_older_than measures against the wall clock, so a literal recent date
    # here ages past the cutoff on its own — pinning "new" to 2026-07-12 made
    # this test start failing on 2026-08-11 with nobody having touched it.
    recent = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    _record(store, "old", "2020-01-01T00:00:00+00:00")
    _record(store, "new", recent)
    assert store.prune_older_than(30) == 1
    assert store.get("old") is None and store.get("new") is not None


def test_stores_wiring_includes_coach(tmp_path, monkeypatch):
    monkeypatch.setenv("JOB_AGG_BACKEND", "sqlite")
    monkeypatch.setenv("JOB_AGG_SQLITE_PATH", str(tmp_path / "t.db"))
    from src.stores import build_stores
    stores = build_stores()
    assert stores.coach is not None
    stores.coach.record(run_id="r1", status="ok")
    assert stores.coach.get("r1")["status"] == "ok"
