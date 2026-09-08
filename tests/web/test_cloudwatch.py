from collections import Counter
from unittest.mock import Mock

import src.web.cloudwatch as cw
from src.web.cloudwatch import (
    LastCycle, PipelineActivity, Tally, TierHeartbeat, TierStats,
    aggregate_log_events, build_family_tallies, build_tier_heartbeats,
    compute_heartbeat, format_ago, tier_stale,
)


def _done(tier, fetched, matched, notified, dur, ts, failed=None):
    return {"message": "invocation_done", "tier": tier, "fetched": fetched,
            "matched": matched, "notified": notified, "duration_ms": dur,
            "failed_sources": failed or [], "_ts_ms": ts}


def _fail(source, error_type, ts):
    return {"message": "fetch_failed", "source": source,
            "error_type": error_type, "_ts_ms": ts}


def test_aggregate_groups_cycle_stats_by_tier():
    events = [
        _done("ats", 1000, 4, 1, 3000, 100),
        _done("ats", 2000, 8, 3, 5000, 200),
        _done("slow", 50, 2, 0, 1000, 150),
    ]
    act = aggregate_log_events(events, window_days=7)
    tiers = {t.tier: t for t in act.tiers}
    assert tiers["ats"].cycles == 2
    assert tiers["ats"].avg_fetched == 1500
    assert tiers["ats"].avg_matched == 6
    assert tiers["slow"].cycles == 1
    assert tiers["slow"].avg_notified == 0


def test_aggregate_tallies_failures_desc():
    events = [
        _fail("ashby:vercel", "DataDome", 10),
        _fail("ashby:vercel", "DataDome", 20),
        _fail("greenhouse:acme", "PoolTimeout", 30),
    ]
    act = aggregate_log_events(events, window_days=7)
    assert act.failures_total == 3
    assert (act.failures_by_type[0].name, act.failures_by_type[0].count) == ("DataDome", 2)
    assert ("greenhouse:acme", 1) in [(t.name, t.count) for t in act.failures_by_connector]


def test_aggregate_tallies_carry_last_seen_recency():
    events = [
        _fail("ashby:vercel", "DataDome", 10),
        _fail("ashby:vercel", "DataDome", 20),
        _fail("greenhouse:acme", "PoolTimeout", 30),
    ]
    act = aggregate_log_events(events, window_days=7)
    by_type = {t.name: t for t in act.failures_by_type}
    assert by_type["DataDome"].count == 2
    assert by_type["DataDome"].last_ms == 20        # most recent occurrence
    by_conn = {t.name: t for t in act.failures_by_connector}
    assert by_conn["greenhouse:acme"].last_ms == 30


def test_build_family_tallies_rolls_connectors_up():
    by_conn = Counter({"workday:a": 5, "workday:b": 8, "greenhouse:x": 2})
    last_conn = {"workday:a": 100, "workday:b": 300, "greenhouse:x": 200}
    fam_types = {
        "workday": Counter({"HTTPStatusError": 12, "ReadTimeout": 1}),
        "greenhouse": Counter({"ReadTimeout": 2}),
    }
    fams = build_family_tallies(by_conn, last_conn, fam_types)
    assert [f.family for f in fams] == ["workday", "greenhouse"]   # count-desc (13 vs 2)
    wd = fams[0]
    assert wd.count == 13 and wd.connectors == 2
    assert wd.top_type == "HTTPStatusError"                        # dominant type
    assert wd.last_ms == 300                                       # newest in family


def test_build_family_tallies_singleton_family_without_colon():
    # a source name with no ':' is its own family; missing fam_types → 'unknown'
    fams = build_family_tallies(Counter({"weird": 3}), {"weird": 9}, {})
    assert fams == [cw.FamilyTally("weird", 3, 1, "unknown", 9)]


def test_aggregate_rolls_failures_up_by_family():
    events = [
        _fail("workday:a", "HTTPStatusError", 10),
        _fail("workday:b", "HTTPStatusError", 40),
        _fail("greenhouse:x", "ReadTimeout", 20),
    ]
    act = aggregate_log_events(events, window_days=7)
    fams = {f.family: f for f in act.failures_by_family}
    assert fams["workday"].count == 2 and fams["workday"].connectors == 2
    assert fams["workday"].top_type == "HTTPStatusError" and fams["workday"].last_ms == 40
    assert [f.family for f in act.failures_by_family] == ["workday", "greenhouse"]


def test_aggregate_last_cycle_is_most_recent_and_flags_failures():
    events = [
        _done("ats", 1, 0, 0, 100, 100),
        _done("ats", 1, 0, 0, 100, 500, failed=["lever:bad"]),
    ]
    act = aggregate_log_events(events, window_days=7)
    assert act.last_cycle.ts_ms == 500
    assert act.last_cycle.ok is False


def test_aggregate_missing_tier_buckets_unknown():
    # a malformed event missing every numeric field must not crash; numerics → 0
    act = aggregate_log_events([{"message": "invocation_done", "_ts_ms": 1}], window_days=7)
    assert {t.tier for t in act.tiers} == {"unknown"}
    assert act.tiers[0].avg_fetched == 0 and act.tiers[0].avg_matched == 0


def test_aggregate_empty_is_safe():
    act = aggregate_log_events([], window_days=7)
    assert act.tiers == [] and act.failures_total == 0 and act.last_cycle is None


def _hb(tier, last_ts, gap_ms, ok=True, degraded=False):
    return TierHeartbeat(
        tier=tier, last=LastCycle(ts_ms=last_ts, ok=ok, degraded=degraded),
        median_gap_ms=gap_ms,
    )


def test_build_tier_heartbeats_last_and_median_gap():
    # ats cycles every ~60s; slow every ~900s. Out-of-order input is sorted.
    per_tier = {
        "ats": [(0, True, False), (120_000, True, False), (60_000, True, False)],
        "slow": [(0, True, False), (900_000, False, False)],
    }
    hbs = {h.tier: h for h in build_tier_heartbeats(per_tier)}
    assert hbs["ats"].last.ts_ms == 120_000
    assert hbs["ats"].median_gap_ms == 60_000
    assert hbs["slow"].last.ts_ms == 900_000
    assert hbs["slow"].last.ok is False
    assert hbs["slow"].median_gap_ms == 900_000


def test_tier_stale_relative_to_cadence():
    now = 10_000_000
    fresh = _hb("ats", now - 120_000, 60_000)      # 2 gaps old → fine
    stale = _hb("ats", now - 3_600_000, 60_000)    # 60 gaps old → stale
    unknown = _hb("ats", now - 10_000_000, 0)      # cadence unknown, <48h silent → not stale
    assert tier_stale(fresh, now) is False
    assert tier_stale(stale, now) is True
    assert tier_stale(unknown, now) is False


def test_tier_stale_respects_floor():
    # A low-cadence tier missing just over one cycle is not flagged before the floor.
    now = 10_000_000
    hb = _hb("slow", now - 6 * 60_000, 60_000)  # 6 min old, gap 60s → 5x=5min floor → stale
    assert tier_stale(hb, now) is True
    near = _hb("slow", now - 4 * 60_000, 60_000)  # 4 min < 5 min floor → not stale
    assert tier_stale(near, now) is False


def test_compute_heartbeat_flags_stalled_tier():
    now = 10_000_000
    act = PipelineActivity(
        window_days=7, tiers=[], failures_by_type=[], failures_by_connector=[],
        failures_total=0, last_cycle=LastCycle(ts_ms=now - 1000, ok=True),
        tier_heartbeats=[
            _hb("ats", now - 3_600_000, 60_000),   # stalled
            _hb("slow", now - 1000, 900_000),       # fresh
        ],
    )
    hb = compute_heartbeat(act, now)
    assert hb.css == "bad"
    assert "ats" in hb.label and "⚠" in hb.label


def test_compute_heartbeat_healthy_reflects_last_cycle():
    now = 10_000_000
    act = PipelineActivity(
        window_days=7, tiers=[], failures_by_type=[], failures_by_connector=[],
        failures_total=0, last_cycle=LastCycle(ts_ms=now - 1000, ok=True),
        tier_heartbeats=[_hb("ats", now - 60_000, 60_000)],
    )
    hb = compute_heartbeat(act, now)
    assert hb.css == "ok" and "✓" in hb.label


def test_compute_heartbeat_no_cycles():
    act = PipelineActivity(
        window_days=7, tiers=[], failures_by_type=[], failures_by_connector=[],
        failures_total=0, last_cycle=None,
    )
    hb = compute_heartbeat(act, 10_000_000)
    assert hb.css == "muted" and hb.label == "no cycles"


def test_format_ago():
    now = 1_000_000_000_000
    assert format_ago(now, now) == "just now"
    assert format_ago(now - 90_000, now) == "1m ago"
    assert format_ago(now - 7_200_000, now) == "2h ago"
    assert format_ago(now - 172_800_000, now) == "2d ago"
    assert format_ago(now + 5_000, now) == "just now"  # future ts clamps to 0


def test_fetch_log_events_parses_and_paginates():
    client = Mock()
    client.filter_log_events.side_effect = [
        {"events": [
            {"timestamp": 111, "message": '{"message":"invocation_done","tier":"ats","matched":3}'},
            {"timestamp": 112, "message": 'PREFIX 2026 {"message":"fetch_failed","source":"x","error_type":"E"}'},
        ], "nextToken": "tok"},
        {"events": [
            {"timestamp": 113, "message": "not json at all"},
        ]},
    ]
    out = cw.fetch_log_events(client, log_group="/g", start_ms=0, end_ms=999)
    assert len(out) == 2  # the unparseable line is skipped
    assert out[0]["message"] == "invocation_done" and out[0]["_ts_ms"] == 111
    assert out[1]["message"] == "fetch_failed" and out[1]["source"] == "x"
    # both pages fetched
    assert client.filter_log_events.call_count == 2


def test_load_pipeline_activity_builds_client_and_aggregates(monkeypatch):
    client = Mock()
    client.filter_log_events.side_effect = [
        {"events": [{"timestamp": 500, "message": '{"message":"invocation_done","tier":"ats","fetched":10,"matched":2,"notified":1,"duration_ms":900}'}]},
    ]
    monkeypatch.setattr(cw.boto3, "client", lambda *a, **k: client)
    act = cw.load_pipeline_activity(log_group="/g", region="us-east-1", window_days=7, now_ms=1_000)
    assert act.tiers[0].tier == "ats" and act.tiers[0].cycles == 1
    assert act.last_cycle.ts_ms == 500
    # window applied: startTime = now - 7d
    _, kwargs = client.filter_log_events.call_args
    assert kwargs["startTime"] == 1_000 - 7 * 86_400_000


def test_aggregate_log_events_leaves_recent_empty():
    act = aggregate_log_events([_done("ats", 1, 0, 0, 100, 100)], window_days=7)
    assert act.recent == []   # recent-cycle detail is local-telemetry only


def test_tier_stale_unknown_cadence_uses_absolute_bound():
    now = 100 * 86_400_000
    h = 3_600_000
    barely = _hb("ats", now - 47 * h, 0)
    over = _hb("ats", now - 49 * h, 0)
    assert tier_stale(barely, now) is False   # under 48h → benefit of the doubt
    assert tier_stale(over, now) is True      # a dead tier can't hide past 48h


def test_merge_tier_last_cycles_appends_out_of_window_tiers():
    windowed = [_hb("ats", 1000, 60_000)]
    merged = cw.merge_tier_last_cycles(windowed, [
        {"tier": "ats", "ts_ms": 1000, "ok": True, "degraded": False},
        {"tier": "slow", "ts_ms": 500, "ok": False, "degraded": True},
    ])
    by_tier = {hb.tier: hb for hb in merged}
    assert by_tier["ats"].median_gap_ms == 60_000      # windowed heartbeat kept
    assert by_tier["slow"].last.ts_ms == 500           # anchor-only tier appended
    assert by_tier["slow"].last.ok is False
    assert by_tier["slow"].last.degraded is True
    assert by_tier["slow"].median_gap_ms == 0
    assert [hb.tier for hb in merged] == ["ats", "slow"]  # tier-sorted


def test_merge_tier_last_cycles_empty_anchor_is_noop():
    windowed = [_hb("ats", 1000, 60_000)]
    assert cw.merge_tier_last_cycles(windowed, []) == windowed
