# tests/test_state_sqlite_health.py
from src.sqlite_db import connect
from src.state_sqlite import SqliteConnectorHealthStore


def _store():
    return SqliteConnectorHealthStore(connect(":memory:"))


def test_record_dead_increments_and_returns_streak():
    s = _store()
    assert s.record_dead("greenhouse:dead") == 1
    assert s.record_dead("greenhouse:dead") == 2


def test_suppress_and_clear():
    s = _store()
    s.record_dead("greenhouse:dead")
    s.mark_suppressed("greenhouse:dead")
    assert s.suppressed_names() == {"greenhouse:dead"}
    assert s.tracked_names() == {"greenhouse:dead"}
    s.clear("greenhouse:dead")
    assert s.suppressed_names() == set()
    assert s.tracked_names() == set()


def test_tracked_includes_mid_streak_not_suppressed():
    s = _store()
    s.record_dead("a")
    assert s.suppressed_names() == set()
    assert s.tracked_names() == {"a"}


def test_mark_backoff_and_backoff_names_window():
    s = _store()
    s.mark_backoff("workable:blaze", until_ms=5_000)
    assert s.backoff_names(now_ms=4_999) == {"workable:blaze"}   # window open
    assert s.backoff_names(now_ms=5_000) == set()               # expired (strict >)
    assert s.suppressed_names() == set()                        # backoff != suppressed


def test_mark_backoff_does_not_disturb_dead_streak():
    s = _store()
    s.record_dead("greenhouse:x")  # streak 1
    s.mark_backoff("greenhouse:x", until_ms=9_999)
    assert s.record_dead("greenhouse:x") == 2  # streak preserved across backoff write
    assert s.backoff_names(now_ms=0) == {"greenhouse:x"}


def test_clear_removes_backoff():
    s = _store()
    s.mark_backoff("workable:blaze", until_ms=10_000)
    s.clear("workable:blaze")
    assert s.backoff_names(now_ms=0) == set()
