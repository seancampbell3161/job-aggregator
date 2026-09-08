# tests/test_ops_alerts.py
from src.notify.ops import OpsAlert
from src.ops_alerts import OpsAlertEvaluator, OpsThresholds
from src.sqlite_db import connect
from src.state_sqlite import (
    SqliteOpsAlertStateStore,
    SqlitePipelineEventsStore,
    _now_ms,
)

H = 3_600_000  # one hour in ms


def _fixture():
    conn = connect(":memory:")
    events = SqlitePipelineEventsStore(conn)
    state = SqliteOpsAlertStateStore(conn)
    ev = OpsAlertEvaluator(state=state, events=events, thresholds=OpsThresholds())
    return conn, events, state, ev


def _insert_cycle(conn, *, ts_ms, tier="ats", new_count=0, llm_failures="[]"):
    conn.execute(
        "INSERT INTO pipeline_events "
        "(ts_ms, tier, fetched, matched, notified, duration_ms, ok, failures, llm_failures, new_count) "
        "VALUES (?,?,?,?,?,?,1,'[]',?,?)",
        (ts_ms, tier, 1, 0, 0, 1, llm_failures, new_count),
    )


def test_alert_state_defaults_and_transitions():
    conn, _, state, _ = _fixture()
    assert state.get("x") == {"active": False, "last_sent_ms": None}
    state.mark_active("x", now_ms=123)
    assert state.get("x") == {"active": True, "last_sent_ms": 123}
    state.mark_recovered("x")
    got = state.get("x")
    assert got["active"] is False


def test_llm_degraded_fires_after_k_consecutive_cycles():
    conn, _, _, ev = _fixture()
    now = _now_ms()
    _insert_cycle(conn, ts_ms=now - 3000, llm_failures='[{"stage":"relevance","error_type":"E"}]')
    assert ev.evaluate_cycle(now_ms=now) == []  # only 1 degraded cycle, K=2
    _insert_cycle(conn, ts_ms=now - 2000, llm_failures='[{"stage":"gap","error_type":"E"}]')
    alerts = ev.evaluate_cycle(now_ms=now)
    assert [a.condition for a in alerts] == ["llm_degraded"]
    assert alerts[0].recovered is False


def test_llm_degraded_recovers_and_respects_cooldown():
    conn, _, _, ev = _fixture()
    now = _now_ms()
    _insert_cycle(conn, ts_ms=now - 3000, llm_failures='[{"stage":"relevance","error_type":"E"}]')
    _insert_cycle(conn, ts_ms=now - 2000, llm_failures='[{"stage":"relevance","error_type":"E"}]')
    assert len(ev.evaluate_cycle(now_ms=now)) == 1
    # still firing, within cooldown -> silent
    assert ev.evaluate_cycle(now_ms=now + 1000) == []
    # still firing, cooldown elapsed -> re-alert
    _insert_cycle(conn, ts_ms=now + 7 * H - 2000, llm_failures='[{"stage":"relevance","error_type":"E"}]')
    _insert_cycle(conn, ts_ms=now + 7 * H - 1000, llm_failures='[{"stage":"relevance","error_type":"E"}]')
    assert [a.condition for a in ev.evaluate_cycle(now_ms=now + 7 * H)] == ["llm_degraded"]
    # clean cycle -> recovery notice, then silence
    _insert_cycle(conn, ts_ms=now + 7 * H + 1000, llm_failures="[]")
    _insert_cycle(conn, ts_ms=now + 7 * H + 2000, llm_failures="[]")
    rec = ev.evaluate_cycle(now_ms=now + 7 * H + 3000)
    assert len(rec) == 1 and rec[0].recovered is True
    assert ev.evaluate_cycle(now_ms=now + 7 * H + 4000) == []


def test_zero_yield_requires_window_coverage():
    conn, _, _, ev = _fixture()
    now = _now_ms()
    # rows spanning only 1h of the 12h window -> NOT firing (just woke up)
    _insert_cycle(conn, ts_ms=now - 1 * H, new_count=0)
    _insert_cycle(conn, ts_ms=now - 1000, new_count=0)
    assert ev.evaluate_cycle(now_ms=now) == []
    # rows spanning the full window, all zero -> fires
    _insert_cycle(conn, ts_ms=now - 12 * H + 60_000, new_count=0)
    alerts = ev.evaluate_cycle(now_ms=now)
    assert [a.condition for a in alerts] == ["zero_yield"]


def test_zero_yield_ignores_pre_migration_rows_and_clears_on_yield():
    conn, _, _, ev = _fixture()
    now = _now_ms()
    _insert_cycle(conn, ts_ms=now - 12 * H + 60_000, new_count=None)  # pre-migration
    _insert_cycle(conn, ts_ms=now - 1000, new_count=0)
    assert ev.evaluate_cycle(now_ms=now) == []  # coverage comes only from non-NULL rows
    _insert_cycle(conn, ts_ms=now - 11 * H, new_count=0)
    assert [a.condition for a in ev.evaluate_cycle(now_ms=now)] == ["zero_yield"]
    _insert_cycle(conn, ts_ms=now - 500, new_count=3)  # yield! -> recovery
    rec = ev.evaluate_cycle(now_ms=now)
    assert len(rec) == 1 and rec[0].recovered is True


def test_staleness_fires_and_recovers():
    conn, _, _, ev = _fixture()
    now = _now_ms()
    assert ev.evaluate_staleness(expected_interval_minutes=2, now_ms=now) is None  # empty table: silent
    _insert_cycle(conn, ts_ms=now - 20 * 60_000)  # 20 min ago; threshold max(6,15)=15 min
    alert = ev.evaluate_staleness(expected_interval_minutes=2, now_ms=now)
    assert alert is not None and alert.condition == "pipeline_stopped"
    _insert_cycle(conn, ts_ms=now - 60_000)  # fresh cycle -> recovery
    rec = ev.evaluate_staleness(expected_interval_minutes=2, now_ms=now)
    assert rec is not None and rec.recovered is True
    assert ev.evaluate_staleness(expected_interval_minutes=2, now_ms=now) is None


# --------------------------------------------------------------------------- #
# Per-tier staleness. pipeline_stopped reads MAX(ts_ms) across all tiers, so a
# single dead tier is invisible to it — the gap that let the ats tier die for
# 12h on 2026-08-02 behind a healthy slow tier.
# --------------------------------------------------------------------------- #

_TIERS = {"ats": 10, "slow": 15, "headless": 45}


def test_tier_staleness_silent_when_all_tiers_fresh():
    conn, _, _, ev = _fixture()
    now = _now_ms()
    for tier in _TIERS:
        _insert_cycle(conn, ts_ms=now - 60_000, tier=tier)
    assert ev.evaluate_tier_staleness(tier_intervals=_TIERS, now_ms=now) == []


def test_tier_staleness_silent_on_empty_table():
    """A fresh install has no baseline and must not alert."""
    _, _, _, ev = _fixture()
    assert ev.evaluate_tier_staleness(tier_intervals=_TIERS, now_ms=_now_ms()) == []


def test_tier_staleness_fires_for_one_dead_tier_and_recovers():
    """Replays the 2026-08-02 outage: ats stops recording (it crashed before
    record_cycle) while slow/headless keep running. pipeline_stopped stays
    silent throughout — this condition is the only thing that catches it."""
    conn, _, _, ev = _fixture()
    now = _now_ms()
    _insert_cycle(conn, ts_ms=now - 12 * H, tier="ats")      # 12h stale
    _insert_cycle(conn, ts_ms=now - 60_000, tier="slow")     # healthy
    _insert_cycle(conn, ts_ms=now - 120_000, tier="headless")

    alerts = ev.evaluate_tier_staleness(tier_intervals=_TIERS, now_ms=now)
    assert [a.condition for a in alerts] == ["tier_stopped:ats"]
    assert alerts[0].recovered is False
    assert "720 min" in alerts[0].body

    # the whole-pipeline watchdog is blind to it — that's the point
    assert ev.evaluate_staleness(expected_interval_minutes=10, now_ms=now) is None

    # cooldown holds while the condition persists
    assert ev.evaluate_tier_staleness(tier_intervals=_TIERS, now_ms=now) == []

    # ats records a cycle again -> one-shot recovery notice
    _insert_cycle(conn, ts_ms=now - 30_000, tier="ats")
    rec = ev.evaluate_tier_staleness(tier_intervals=_TIERS, now_ms=now)
    assert len(rec) == 1 and rec[0].recovered is True
    assert rec[0].condition == "tier_stopped:ats"
    assert ev.evaluate_tier_staleness(tier_intervals=_TIERS, now_ms=now) == []


def test_tier_staleness_suppressed_when_every_tier_is_stale():
    """A whole-poller outage is pipeline_stopped's job. Without this guard one
    incident would fan out into an alert per tier."""
    conn, _, _, ev = _fixture()
    now = _now_ms()
    for tier in _TIERS:
        _insert_cycle(conn, ts_ms=now - 12 * H, tier=tier)
    assert ev.evaluate_tier_staleness(tier_intervals=_TIERS, now_ms=now) == []
    # ...and the whole-pipeline condition does fire for it
    assert ev.evaluate_staleness(expected_interval_minutes=10, now_ms=now) is not None


def test_tier_staleness_uses_per_tier_cadence():
    """45 min since a headless cycle is healthy (interval 45, threshold 135);
    the same age on ats (interval 10, threshold 30) is stale."""
    conn, _, _, ev = _fixture()
    now = _now_ms()
    _insert_cycle(conn, ts_ms=now - 45 * 60_000, tier="headless")
    _insert_cycle(conn, ts_ms=now - 45 * 60_000, tier="ats")
    _insert_cycle(conn, ts_ms=now - 60_000, tier="slow")
    alerts = ev.evaluate_tier_staleness(tier_intervals=_TIERS, now_ms=now)
    assert [a.condition for a in alerts] == ["tier_stopped:ats"]


def test_tier_staleness_ignores_tiers_with_no_history():
    """A tier that has never run (e.g. headless disabled) must not alert."""
    conn, _, _, ev = _fixture()
    now = _now_ms()
    _insert_cycle(conn, ts_ms=now - 12 * H, tier="ats")
    _insert_cycle(conn, ts_ms=now - 60_000, tier="slow")
    alerts = ev.evaluate_tier_staleness(tier_intervals=_TIERS, now_ms=now)
    assert [a.condition for a in alerts] == ["tier_stopped:ats"]
