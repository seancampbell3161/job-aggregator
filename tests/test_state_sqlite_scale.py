# tests/test_state_sqlite_scale.py
from src.cadence import DUE_SLACK_MS
from src.sqlite_db import connect
from src.state_sqlite import SqliteConnectorScheduleStore, SqliteEvaluatedPostingsStore

DAY = 86_400


V1, V2 = "0.17.0", "0.18.0"


def test_record_and_known_roundtrip():
    s = SqliteEvaluatedPostingsStore(connect(":memory:"))
    assert s.record_many(["a", "b"], generation=3, app_version=V1, now_s=1_000) == 2
    assert s.known(["a", "b", "c"]) == {"a": (3, V1), "b": (3, V1)}


def test_record_replaces_generation_and_version():
    s = SqliteEvaluatedPostingsStore(connect(":memory:"))
    s.record_many(["a"], generation=1, app_version=V1, now_s=1_000)
    s.record_many(["a"], generation=2, app_version=V1, now_s=2_000)
    assert s.known(["a"]) == {"a": (2, V1)}
    s.record_many(["a"], generation=2, app_version=V2, now_s=3_000)
    assert s.known(["a"]) == {"a": (2, V2)}


def test_known_chunks_large_inputs():
    s = SqliteEvaluatedPostingsStore(connect(":memory:"))
    ids = [f"g:{i}" for i in range(1_234)]
    s.record_many(ids, generation=1, app_version=V1, now_s=1)
    assert len(s.known(ids)) == 1_234


def test_prune_drops_old_other_generation_and_other_version_rows():
    s = SqliteEvaluatedPostingsStore(connect(":memory:"))
    now = 100 * DAY
    s.record_many(["old"], generation=5, app_version=V2, now_s=now - 31 * DAY)
    s.record_many(["stale_gen"], generation=4, app_version=V2, now_s=now)
    s.record_many(["stale_ver"], generation=5, app_version=V1, now_s=now)
    s.record_many(["keep"], generation=5, app_version=V2, now_s=now - DAY)
    assert s.prune(current_generation=5, current_app_version=V2,
                   max_age_days=30, now_s=now) == 3
    assert s.known(["old", "stale_gen", "stale_ver", "keep"]) == {"keep": (5, V2)}


def test_record_many_empty_is_noop():
    s = SqliteEvaluatedPostingsStore(connect(":memory:"))
    assert s.record_many([], generation=1, app_version=V1) == 0


def test_unknown_boards_are_due_and_quiet_boards_back_off():
    s = SqliteConnectorScheduleStore(connect(":memory:"))
    assert s.not_due(0) == set()
    s.record_polls({"greenhouse:a": 0, "greenhouse:b": 2}, base_interval_s=600, now_ms=1_000_000)
    ia, due_a, new_a = s.get("greenhouse:a")
    ib, due_b, new_b = s.get("greenhouse:b")
    assert (ia, ib) == (1200, 600)
    assert due_a == 1_000_000 + 1_200_000 - DUE_SLACK_MS and new_a is None
    assert due_b == 1_000_000 + 600_000 - DUE_SLACK_MS and new_b == 1_000_000
    assert s.not_due(1_000_000 + 600_000) == {"greenhouse:a"}


def test_force_due_resets_even_unpolled_names():
    s = SqliteConnectorScheduleStore(connect(":memory:"))
    s.record_polls({"x": 0}, base_interval_s=600, now_ms=0)
    s.record_polls({"x": 0}, base_interval_s=600, now_ms=0)   # 2400
    s.record_polls({}, base_interval_s=600, now_ms=10, force_due=["x"])
    assert s.get("x")[0] == 600
    assert "x" not in s.not_due(10 + 600_000)


def test_force_due_unpolled_is_due_now_and_keeps_last_new():
    s = SqliteConnectorScheduleStore(connect(":memory:"))
    s.record_polls({"x": 1}, base_interval_s=600, now_ms=5)
    s.record_polls({}, base_interval_s=600, now_ms=100, force_due=["x", "y"])
    assert s.get("x") == (600, 100, 5)
    assert s.get("y") == (600, 100, None)
    assert s.not_due(100) == set()


def test_quiet_poll_keeps_previous_last_new_and_empty_is_noop():
    s = SqliteConnectorScheduleStore(connect(":memory:"))
    s.record_polls({}, base_interval_s=600, now_ms=1)
    assert s.get("nothing") is None
    s.record_polls({"x": 4}, base_interval_s=600, now_ms=10)
    s.record_polls({"x": 0}, base_interval_s=600, now_ms=20)
    assert s.get("x")[2] == 10


def test_record_polls_handles_many_names():
    s = SqliteConnectorScheduleStore(connect(":memory:"))
    polled = {f"b:{i}": 0 for i in range(1_234)}
    s.record_polls(polled, base_interval_s=600, now_ms=0)
    s.record_polls(polled, base_interval_s=600, now_ms=0)
    assert s.get("b:1233")[0] == 2400


def test_generation_marker():
    s = SqliteConnectorScheduleStore(connect(":memory:"))
    assert s.generation("ats") is None
    s.set_generation("ats", 7)
    assert s.generation("ats") == 7 and s.generation("slow") is None
    s.set_generation("ats", 8)
    assert s.generation("ats") == 8
