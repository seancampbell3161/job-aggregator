from collections import Counter
from typing import get_args

import src.web.pipeline_activity as pa
from src.models import Tier
from src.web.pipeline_activity import (
    TIER_LABELS, LastCycle, PipelineActivity, TierHeartbeat,
    build_family_tallies, build_tier_heartbeats,
    compute_heartbeat, format_ago, tier_label, tier_stale,
)


def test_every_tier_has_a_plain_name():
    for t in (*get_args(Tier), "digest"):
        assert t in TIER_LABELS
    assert tier_label("ats") == "Job boards"
    assert tier_label("mystery") == "Mystery"


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
    assert fams == [pa.FamilyTally("weird", 3, 1, "unknown", 9)]


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
    assert "Job boards stalled" in hb.label and "⚠" in hb.label


def test_compute_heartbeat_healthy_reflects_last_cycle():
    now = 10_000_000
    act = PipelineActivity(
        window_days=7, tiers=[], failures_by_type=[], failures_by_connector=[],
        failures_total=0, last_cycle=LastCycle(ts_ms=now - 1000, ok=True),
        tier_heartbeats=[_hb("ats", now - 60_000, 60_000)],
    )
    hb = compute_heartbeat(act, now)
    assert hb.css == "ok" and "✓" in hb.label


def test_compute_heartbeat_degraded_says_ai_scoring():
    now = 10_000_000
    act = PipelineActivity(
        window_days=7, tiers=[], failures_by_type=[], failures_by_connector=[],
        failures_total=0, last_cycle=LastCycle(ts_ms=now - 1000, ok=True, degraded=True),
        tier_heartbeats=[_hb("ats", now - 60_000, 60_000)],
    )
    hb = compute_heartbeat(act, now)
    assert hb.css == "warn" and "AI scoring had errors" in hb.label and "LLM" not in hb.label


def test_compute_heartbeat_no_cycles():
    act = PipelineActivity(
        window_days=7, tiers=[], failures_by_type=[], failures_by_connector=[],
        failures_total=0, last_cycle=None,
    )
    hb = compute_heartbeat(act, 10_000_000)
    assert hb.css == "muted" and hb.label == "no checks yet"


def test_format_ago():
    now = 1_000_000_000_000
    assert format_ago(now, now) == "just now"
    assert format_ago(now - 90_000, now) == "1m ago"
    assert format_ago(now - 7_200_000, now) == "2h ago"
    assert format_ago(now - 172_800_000, now) == "2d ago"
    assert format_ago(now + 5_000, now) == "just now"  # future ts clamps to 0


def test_tier_stale_unknown_cadence_uses_absolute_bound():
    now = 100 * 86_400_000
    h = 3_600_000
    barely = _hb("ats", now - 47 * h, 0)
    over = _hb("ats", now - 49 * h, 0)
    assert tier_stale(barely, now) is False   # under 48h → benefit of the doubt
    assert tier_stale(over, now) is True      # a dead tier can't hide past 48h


def test_merge_tier_last_cycles_appends_out_of_window_tiers():
    windowed = [_hb("ats", 1000, 60_000)]
    merged = pa.merge_tier_last_cycles(windowed, [
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
    assert pa.merge_tier_last_cycles(windowed, []) == windowed
