# tests/test_state_sqlite_scale.py
from src.sqlite_db import connect
from src.state_sqlite import SqliteEvaluatedPostingsStore

DAY = 86_400


def test_record_and_known_roundtrip():
    s = SqliteEvaluatedPostingsStore(connect(":memory:"))
    assert s.record_many(["a", "b"], generation=3, now_s=1_000) == 2
    assert s.known(["a", "b", "c"]) == {"a": 3, "b": 3}


def test_record_replaces_generation():
    s = SqliteEvaluatedPostingsStore(connect(":memory:"))
    s.record_many(["a"], generation=1, now_s=1_000)
    s.record_many(["a"], generation=2, now_s=2_000)
    assert s.known(["a"]) == {"a": 2}


def test_known_chunks_large_inputs():
    s = SqliteEvaluatedPostingsStore(connect(":memory:"))
    ids = [f"g:{i}" for i in range(1_234)]
    s.record_many(ids, generation=1, now_s=1)
    assert len(s.known(ids)) == 1_234


def test_prune_drops_old_and_other_generation_rows():
    s = SqliteEvaluatedPostingsStore(connect(":memory:"))
    now = 100 * DAY
    s.record_many(["old"], generation=5, now_s=now - 31 * DAY)
    s.record_many(["stale_gen"], generation=4, now_s=now)
    s.record_many(["keep"], generation=5, now_s=now - DAY)
    assert s.prune(current_generation=5, max_age_days=30, now_s=now) == 2
    assert s.known(["old", "stale_gen", "keep"]) == {"keep": 5}


def test_record_many_empty_is_noop():
    s = SqliteEvaluatedPostingsStore(connect(":memory:"))
    assert s.record_many([], generation=1) == 0
