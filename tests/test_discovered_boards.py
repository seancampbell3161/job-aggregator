from __future__ import annotations

from src.sqlite_db import connect
from src.state_sqlite import SqliteDiscoveredBoardsStore

_IDENT = {"tenant": "3m", "region": "wd1", "site": "Search"}


def _store():
    return SqliteDiscoveredBoardsStore(connect(":memory:"))


def test_upsert_ok_and_list_healthy():
    s = _store()
    s.upsert_ok("3m.com", name="3M", family="workday", identity=_IDENT,
                connector_name="workday:3m:Search", company="3M")
    healthy = s.list_healthy()
    assert len(healthy) == 1
    b = healthy[0]
    assert b.domain == "3m.com" and b.family == "workday"
    assert b.identity == _IDENT
    assert b.connector_name == "workday:3m:Search"
    assert b.status == "ok" and b.failure_streak == 0


def test_upsert_result_and_quarantine():
    s = _store()
    for _ in range(4):
        s.upsert_result("dead.com", name="Dead", status="error", quarantine_threshold=5)
    assert s.get("dead.com").status == "error"       # 4 < 5
    s.upsert_result("dead.com", name="Dead", status="error", quarantine_threshold=5)
    assert s.get("dead.com").status == "quarantined"      # 5th
    assert s.list_healthy() == []                          # non-ok excluded


def test_upsert_result_benign_not_found_never_quarantines():
    """Only status='error' streaks toward quarantine — benign not_found/
    unsupported seeds must not poison the seed list after 5 sweeps."""
    s = _store()
    for _ in range(5):
        s.upsert_result("quiet.com", name="Quiet", status="not_found", quarantine_threshold=5)
    row = s.get("quiet.com")
    assert row.status == "not_found"
    assert row.failure_streak == 0


def test_ok_overwrites_prior_failure():
    s = _store()
    s.upsert_result("late.com", name="Late", status="not_found")
    s.upsert_ok("late.com", name="Late", family="taleo",
                identity={"tenant": "cinfin", "section": "ex"},
                connector_name="taleo:cinfin:ex", company="Cincinnati Financial")
    b = s.get("late.com")
    assert b.status == "ok" and b.failure_streak == 0


def test_stale_before():
    s = _store()
    s.upsert_ok("a.com", name="A", family="workday", identity=_IDENT, connector_name="workday:a:x")
    # a far-future cutoff → everything is stale; a past cutoff → nothing
    assert [b.domain for b in s.stale_before("9999-01-01")] == ["a.com"]
    assert s.stale_before("0001-01-01") == []


def test_boards_store_present_on_sqlite_absent_on_dynamo(monkeypatch, tmp_path):
    from src.stores import build_stores
    # Isolate the DB — never open the real data/job_aggregator.db dev database.
    monkeypatch.setenv("JOB_AGG_BACKEND", "sqlite")
    monkeypatch.setenv("JOB_AGG_SQLITE_PATH", str(tmp_path / "t.db"))
    assert build_stores().boards is not None
    # DynamoDB backend leaves the SQLite-only boards store absent.
    monkeypatch.setenv("JOB_AGG_BACKEND", "dynamodb")
    assert build_stores().boards is None


from src.sqlite_db import connect as _connect
from src.state_sqlite import SqliteDiscoveredBoardsStore as _BoardsStore


def _bstore():
    return _BoardsStore(_connect(":memory:"))


def test_board_candidate_lifecycle():
    b = _bstore()
    b.upsert_candidate("acme.wd5.myworkdayjobs.com", name="Acme", family="workday",
                       identity={"tenant": "acme", "region": "wd5", "site": "External"},
                       connector_name="workday:acme:External", company="Acme",
                       origin="hiringcafe")
    rows = b.list_candidates()
    assert len(rows) == 1
    assert rows[0].status == "candidate"
    assert rows[0].origin == "hiringcafe"
    assert rows[0].sighted_at
    assert rows[0].identity == {"tenant": "acme", "region": "wd5", "site": "External"}
    assert b.list_healthy() == []  # candidates never poll


def test_board_candidate_noops_on_existing_row():
    b = _bstore()
    b.upsert_ok("acme.wd5.myworkdayjobs.com", name="Acme", family="workday",
                identity={"tenant": "acme", "region": "wd5", "site": "External"},
                connector_name="workday:acme:External")
    b.upsert_candidate("acme.wd5.myworkdayjobs.com", name="Imposter", family="workday",
                       identity={}, connector_name=None)
    assert b.get("acme.wd5.myworkdayjobs.com").status == "ok"


def test_board_candidate_failed_streaks_then_quarantines():
    b = _bstore()
    b.upsert_candidate("x.wd1.myworkdayjobs.com", name="X", family="workday",
                       identity={"tenant": "x", "region": "wd1", "site": "S"},
                       connector_name="workday:x:S")
    b.upsert_candidate_failed("x.wd1.myworkdayjobs.com", quarantine_threshold=2)
    row = b.get("x.wd1.myworkdayjobs.com")
    assert row.status == "candidate" and row.failure_streak == 1
    assert row.identity == {"tenant": "x", "region": "wd1", "site": "S"}  # preserved
    b.upsert_candidate_failed("x.wd1.myworkdayjobs.com", quarantine_threshold=2)
    assert b.get("x.wd1.myworkdayjobs.com").status == "quarantined"
    assert b.list_candidates() == []
    # no-op on non-candidate rows
    b.upsert_candidate_failed("x.wd1.myworkdayjobs.com", quarantine_threshold=2)
    assert b.get("x.wd1.myworkdayjobs.com").failure_streak == 2
