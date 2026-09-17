"""Tests that OpsProvider degrades gracefully when data sources are unavailable."""
from __future__ import annotations

import pytest

from src.sqlite_db import connect
from src.state_sqlite import SqlitePipelineEventsStore, _now_ms
from src.web.ops import OpsProvider, aggregate_event_rows


class _Stub:
    def list_all(self): return []
    def list_matches(self): return []
    def list_suppressed(self): return []
    def suppressed_names(self): return set()


def test_ops_provider_has_no_cloudwatch_mode():
    import inspect
    from src.web.ops import OpsProvider
    params = inspect.signature(OpsProvider).parameters
    assert "local_mode" not in params
    assert "log_group" not in params and "region" not in params


def test_cycles_returns_none_without_events_store():
    """No events store: cycles() is None."""
    ops = OpsProvider(discovered=_Stub(), seen=_Stub(), health=_Stub())
    assert ops.cycles() is None


def test_cycles_aggregates_local_events():
    """events store wired: cycles() aggregates SQLite telemetry."""
    events = SqlitePipelineEventsStore(connect(":memory:"))
    events.record_cycle(tier="ats", fetched=10, matched=2, notified=1, duration_ms=1000)
    events.record_cycle(
        tier="ats", fetched=20, matched=4, notified=0, duration_ms=2000,
        failures=[{"source": "greenhouse:x", "error_type": "ReadTimeout"}],
    )
    ops = OpsProvider(discovered=_Stub(), seen=_Stub(), health=_Stub(), events=events)
    activity = ops.cycles()
    assert activity is not None
    assert [t.tier for t in activity.tiers] == ["ats"]
    ats = activity.tiers[0]
    assert ats.cycles == 2 and ats.avg_fetched == 15
    assert activity.failures_total == 1
    assert [(t.name, t.count) for t in activity.failures_by_type] == [("ReadTimeout", 1)]
    assert [(t.name, t.count) for t in activity.failures_by_connector] == [("greenhouse:x", 1)]
    assert activity.last_cycle is not None and activity.last_cycle.ok is False


def test_last_success_reflects_ok_cycles():
    events = SqlitePipelineEventsStore(connect(":memory:"))
    ops = OpsProvider(discovered=_Stub(), seen=_Stub(), health=_Stub(), events=events)
    assert ops.last_success() is None
    events.record_cycle(tier="ats", fetched=1, matched=0, notified=0, duration_ms=1)
    assert ops.last_success() is not None


def test_aggregate_event_rows_tallies_carry_recency():
    rows = [
        {"ts_ms": 1000, "tier": "ats", "fetched": 1, "matched": 0, "notified": 0,
         "duration_ms": 5, "ok": False,
         "failures": [{"source": "greenhouse:x", "error_type": "ReadTimeout"}],
         "llm_failures": [], "new_count": 0},
        {"ts_ms": 2000, "tier": "ats", "fetched": 1, "matched": 0, "notified": 0,
         "duration_ms": 5, "ok": False,
         "failures": [{"source": "greenhouse:x", "error_type": "ReadTimeout"},
                      {"source": "lever:y", "error_type": "HTTP500"}],
         "llm_failures": [{"stage": "relevance", "error_type": "ConnectError"}],
         "new_count": 0},
    ]
    act = aggregate_event_rows(rows, window_days=7)
    by_type = {t.name: t for t in act.failures_by_type}
    assert by_type["ReadTimeout"].count == 2 and by_type["ReadTimeout"].last_ms == 2000
    assert by_type["HTTP500"].last_ms == 2000
    by_conn = {t.name: t for t in act.failures_by_connector}
    assert by_conn["greenhouse:x"].last_ms == 2000
    stages = {s.stage: s for s in act.llm_failures_by_stage}
    assert stages["relevance"].last_ms == 2000


def test_aggregate_event_rows_empty():
    activity = aggregate_event_rows([], window_days=7)
    assert activity.tiers == []
    assert activity.failures_total == 0
    assert activity.last_cycle is None
    assert activity.failures_by_family == []


def test_aggregate_event_rows_rolls_failures_up_by_family():
    rows = [
        {"ts_ms": 1000, "tier": "ats", "fetched": 1, "matched": 0, "notified": 0,
         "duration_ms": 5, "ok": False,
         "failures": [{"source": "workday:a", "error_type": "HTTPStatusError"},
                      {"source": "greenhouse:x", "error_type": "ReadTimeout"}],
         "llm_failures": [], "new_count": 0},
        {"ts_ms": 5000, "tier": "ats", "fetched": 1, "matched": 0, "notified": 0,
         "duration_ms": 5, "ok": False,
         "failures": [{"source": "workday:b", "error_type": "HTTPStatusError"}],
         "llm_failures": [], "new_count": 0},
    ]
    act = aggregate_event_rows(rows, window_days=7)
    fams = {f.family: f for f in act.failures_by_family}
    assert fams["workday"].count == 2 and fams["workday"].connectors == 2
    assert fams["workday"].top_type == "HTTPStatusError"
    assert fams["workday"].last_ms == 5000            # newest across the family
    assert fams["greenhouse"].count == 1 and fams["greenhouse"].connectors == 1
    assert act.failures_by_family[0].family == "workday"   # count-desc order


def test_aggregate_event_rows_rolls_up_llm_failures():
    events = SqlitePipelineEventsStore(connect(":memory:"))
    events.record_cycle(
        tier="ats", fetched=10, matched=3, notified=1, duration_ms=1000,
        llm_failures=[
            {"stage": "relevance", "error_type": "ConnectError"},
            {"stage": "relevance", "error_type": "TimeoutError"},
            {"stage": "gap", "error_type": "ConnectError"},
        ],
    )
    events.record_cycle(  # a clean cycle
        tier="ats", fetched=5, matched=1, notified=1, duration_ms=500,
    )
    activity = aggregate_event_rows(events.recent_cycles(7), window_days=7)

    assert activity.llm_failures_total == 3
    assert activity.llm_degraded_cycles == 1
    by_stage = {s.stage: s for s in activity.llm_failures_by_stage}
    assert by_stage["relevance"].total == 2
    assert dict(by_stage["relevance"].by_type) == {"ConnectError": 1, "TimeoutError": 1}
    assert by_stage["gap"].total == 1
    # The most-recent cycle (clean) drives last_cycle: ok and NOT degraded.
    assert activity.last_cycle.ok is True
    assert activity.last_cycle.degraded is False


def test_aggregate_event_rows_marks_last_cycle_degraded():
    events = SqlitePipelineEventsStore(connect(":memory:"))
    events.record_cycle(
        tier="ats", fetched=10, matched=1, notified=1, duration_ms=100,
        llm_failures=[{"stage": "relevance", "error_type": "ConnectError"}],
    )
    activity = aggregate_event_rows(events.recent_cycles(7), window_days=7)
    assert activity.last_cycle.ok is True        # fetch-ok
    assert activity.last_cycle.degraded is True  # but LLM-degraded


def test_aggregate_event_rows_empty_has_zero_llm_failures():
    activity = aggregate_event_rows([], window_days=7)
    assert activity.llm_failures_total == 0
    assert activity.llm_failures_by_stage == []
    assert activity.llm_degraded_cycles == 0


def _template_env():
    """Bare Jinja env for direct template renders — must mirror create_app's
    filter registration or _ops_cycles.html fails to compile."""
    from jinja2 import Environment, FileSystemLoader
    from src.web.pipeline_activity import format_ago
    env = Environment(loader=FileSystemLoader("src/web/templates"), autoescape=True)
    env.filters["ago"] = format_ago
    return env


def test_ops_cycles_template_renders_llm_failures_and_degraded_heartbeat():
    from types import SimpleNamespace
    from src.web.pipeline_activity import Heartbeat, StageFailures

    env = _template_env()
    activity = SimpleNamespace(
        window_days=7,
        tiers=[SimpleNamespace(tier="ats", cycles=2, avg_fetched=15.0,
                               avg_matched=2.0, avg_notified=1.0, avg_duration_ms=1000.0)],
        failures_total=0, failures_by_type=[], failures_by_connector=[],
        failures_by_family=[],
        llm_failures_total=15,
        llm_degraded_cycles=3,
        llm_failures_by_stage=[
            StageFailures(stage="relevance", total=12, by_type=[("ConnectError", 12)]),
            StageFailures(stage="gap", total=3, by_type=[("ConnectError", 3)]),
        ],
    )
    # heartbeat is computed in Python (compute_heartbeat) and passed in; the
    # template just renders its css + label.
    html = env.get_template("_ops_cycles.html").render(
        activity=activity, heartbeat=Heartbeat("warn", "2m ago ✓ LLM⚠"),
        tier_health={"ats": {"ago": "2m ago", "stale": False, "ok": True, "degraded": True}},
    )

    assert "LLM failures (15)" in html
    assert "3 degraded" in html
    assert "relevance" in html and "ConnectError" in html
    assert "LLM⚠" in html              # degraded marker on the heartbeat
    assert 'class="v warn"' in html     # amber, not green/red


def test_ops_cycles_template_clean_heartbeat_when_not_degraded():
    from types import SimpleNamespace
    from src.web.pipeline_activity import Heartbeat

    env = _template_env()
    activity = SimpleNamespace(
        window_days=7, tiers=[], failures_total=0, failures_by_type=[],
        failures_by_connector=[], failures_by_family=[], llm_failures_total=0,
        llm_degraded_cycles=0, llm_failures_by_stage=[],
    )
    html = env.get_template("_ops_cycles.html").render(
        activity=activity, heartbeat=Heartbeat("ok", "1m ago ✓"), tier_health={})
    assert "LLM⚠" not in html
    assert "✓" in html


def test_ops_cycles_template_shows_stalled_tier_in_last_column():
    from types import SimpleNamespace
    from src.web.pipeline_activity import Heartbeat

    env = _template_env()
    activity = SimpleNamespace(
        window_days=7,
        tiers=[SimpleNamespace(tier="ats", cycles=100, avg_fetched=9000.0,
                               avg_matched=0.0, avg_notified=0.0, avg_duration_ms=12000.0)],
        failures_total=0, failures_by_type=[], failures_by_connector=[],
        failures_by_family=[],
        llm_failures_total=0, llm_degraded_cycles=0, llm_failures_by_stage=[],
    )
    html = env.get_template("_ops_cycles.html").render(
        activity=activity, heartbeat=Heartbeat("bad", "ats stalled · 2d ago ⚠"),
        tier_health={"ats": {"ago": "2d ago", "stale": True, "ok": False, "degraded": False}},
    )
    assert "stalled" in html              # per-tier "last" column flags it
    assert 'class="v bad"' in html        # overall heartbeat is red


def test_ops_cycles_template_renders_family_triage_active_and_stale():
    from types import SimpleNamespace
    from src.web.pipeline_activity import FamilyTally, Heartbeat, Tally

    env = _template_env()
    now = 1_000_000_000_000
    dim = now - 24 * 3_600_000
    activity = SimpleNamespace(
        window_days=7, tiers=[], failures_total=1199, failures_by_type=[],
        failures_by_family=[
            FamilyTally("workable", 786, 7, "ConnectError", now - 6 * 86_400_000),   # stale, biggest
            FamilyTally("workday", 412, 61, "HTTPStatusError", now - 3_600_000),      # active
            FamilyTally("oraclecloud", 1, 1, "HTTPStatusError", now - 3_600_000),     # active, low sev
        ],
        failures_by_connector=[Tally("workday:a", 412, now - 3_600_000)],
        llm_failures_total=0, llm_degraded_cycles=0, llm_failures_by_stage=[],
    )
    html = env.get_template("_ops_cycles.html").render(
        activity=activity, heartbeat=Heartbeat("ok", "1m ago ✓"),
        tier_health={}, now_ms=now, dim_before_ms=dim,
    )
    # active families render as severity-graded rows; the 6-day-old workable burst
    # is demoted behind the stale disclosure rather than dominating the view.
    assert "sev-hi" in html and "sev-lo" in html
    assert "workday" in html and ">61 boards</span>" in html and "HTTPStatusError" in html
    assert ">1 board</span>" in html                        # singular pluralization (oraclecloud)
    assert "stale · 1 family" in html
    assert "all connectors (1)" in html                    # layer-3 full detail present


def test_aggregate_event_rows_builds_recent_newest_first():
    rows = [
        {"ts_ms": i * 1000, "tier": "ats", "fetched": i, "matched": 0, "notified": 0,
         "duration_ms": 5, "ok": True, "failures": [], "llm_failures": [],
         "new_count": None}
        for i in range(1, 26)
    ]
    rows[24] = dict(rows[24], ok=False,
                    failures=[{"source": "lever:y", "error_type": "HTTP500"}],
                    llm_failures=[{"stage": "gap", "error_type": "TimeoutError"}])
    act = aggregate_event_rows(rows, window_days=7)
    assert len(act.recent) == 20                      # capped at 20
    assert act.recent[0].ts_ms == 25_000              # newest first
    assert act.recent[0].ok is False
    assert act.recent[0].degraded is True
    assert act.recent[0].failures == [("lever:y", "HTTP500")]
    assert act.recent[0].new_count is None
    assert act.recent[-1].ts_ms == 6_000              # 20th-newest


def test_aggregate_event_rows_empty_recent():
    assert aggregate_event_rows([], window_days=7).recent == []


def test_cycles_merges_out_of_window_tier_anchor():
    """A tier whose rows all aged out of the window resurfaces from its anchor
    row and reads as stale — the stall warning must never silently vanish."""
    events = SqlitePipelineEventsStore(connect(":memory:"))
    events.record_cycle(tier="ats", fetched=1, matched=0, notified=0, duration_ms=1)
    old = _now_ms() - 12 * 86_400_000
    events._conn.execute(
        "INSERT INTO pipeline_events (ts_ms, tier, fetched, matched, notified, duration_ms, ok, failures, llm_failures) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (old, "slow", 1, 0, 0, 1, 1, "[]", "[]"),
    )
    ops = OpsProvider(discovered=_Stub(), seen=_Stub(), health=_Stub(), events=events)
    activity = ops.cycles()
    tiers = {hb.tier: hb for hb in activity.tier_heartbeats}
    assert "slow" in tiers
    assert tiers["slow"].last.ts_ms == old
    from src.web.pipeline_activity import tier_stale
    assert tier_stale(tiers["slow"], _now_ms()) is True


def test_cycles_survives_anchor_query_failure(monkeypatch):
    events = SqlitePipelineEventsStore(connect(":memory:"))
    events.record_cycle(tier="ats", fetched=1, matched=0, notified=0, duration_ms=1)
    monkeypatch.setattr(events, "last_cycle_per_tier",
                        lambda: (_ for _ in ()).throw(RuntimeError("db locked")))
    ops = OpsProvider(discovered=_Stub(), seen=_Stub(), health=_Stub(), events=events)
    activity = ops.cycles()
    assert activity is not None                          # merge degrades, panel survives
    assert [hb.tier for hb in activity.tier_heartbeats] == ["ats"]
