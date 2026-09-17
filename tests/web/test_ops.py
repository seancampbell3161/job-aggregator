from datetime import datetime, timezone

import pytest

from src.sqlite_db import connect
from src.state import DiscoveredSlug
from src.state_sqlite import SqliteConnectorHealthStore, SqliteDiscoveredSlugsStore, SqliteSeenJobsStore
from src.web.ops import (
    HealthSummary, OpsProvider, ScoreAnalytics, connector_health, score_analytics,
)


def _slug(slug, status, fails=0, postings=0):
    return DiscoveredSlug(
        connector_name=f"greenhouse:{slug}", ats_family="greenhouse", slug=slug,
        company_name=None, discovered_at="2026-06-01T00:00:00+00:00",
        last_validated_at="2026-06-16T00:00:00+00:00", validation_status=status,
        consecutive_failures=fails, last_posting_count=postings,
    )


def test_connector_health_counts_and_unhealthy_sorted():
    rows = [
        _slug("a", "ok", postings=5), _slug("b", "ok"),
        _slug("c", "failed", fails=2), _slug("d", "failed", fails=4),
        _slug("e", "quarantined", fails=5), _slug("f", "no_match"),
    ]
    h = connector_health(rows)
    # "failed" is the real validation_status DiscoveredSlugsStore writes
    assert (h.ok, h.failed, h.quarantined, h.no_match) == (2, 2, 1, 1)
    # unhealthy = only failed/quarantined (actual broken connectors), sorted by
    # consecutive_failures desc. no_match is a discovery negative-cache entry, NOT
    # a connector, so it must be excluded (there can be thousands of them).
    assert [r.slug for r in h.unhealthy] == ["e", "d", "c"]
    assert all(r.validation_status != "no_match" for r in h.unhealthy)


def test_connector_health_empty():
    h = connector_health([])
    assert h == HealthSummary(ok=0, failed=0, quarantined=0, no_match=0, unhealthy=[])


def test_score_analytics_histogram_status_and_recent():
    now = datetime(2026, 6, 20, tzinfo=timezone.utc)
    rows = [
        {"score": 8, "status": "new", "first_seen": "2026-06-19T00:00:00+00:00"},
        {"score": 8, "status": "applied", "first_seen": "2026-06-18T00:00:00+00:00"},
        {"score": 3, "status": "dismissed", "first_seen": "2026-06-01T00:00:00+00:00"},  # old
        {"score": None, "status": "new", "first_seen": "2026-06-19T00:00:00+00:00"},     # unscored
    ]
    a = score_analytics(rows, now=now, window_days=7)
    assert a.total == 4
    assert a.histogram[8] == 2 and a.histogram[3] == 1
    assert sum(a.histogram) == 3                  # unscored excluded from histogram
    assert a.by_status == {"new": 2, "applied": 1, "dismissed": 1}
    assert a.recent == 3                          # the 06-01 row is outside 7d


def test_score_analytics_empty():
    a = score_analytics([], now=datetime(2026, 6, 20, tzinfo=timezone.utc))
    assert a.total == 0 and sum(a.histogram) == 0 and a.recent == 0


def test_score_analytics_excludes_bool_scores():
    now = datetime(2026, 6, 20, tzinfo=timezone.utc)
    rows = [{"score": True, "status": "new", "first_seen": "2026-06-19T00:00:00+00:00"}]
    a = score_analytics(rows, now=now)
    assert sum(a.histogram) == 0  # a bool must not be treated as score 0/1
    assert a.total == 1


def test_score_analytics_includes_suppressed_in_histogram_only():
    """Suppressed scores fill the histogram's low end, but total/recent/by_status
    stay notified-only so the funnel stats keep their meaning."""
    now = datetime(2026, 6, 20, tzinfo=timezone.utc)
    matches = [
        {"score": 8, "status": "new", "first_seen": "2026-06-19T00:00:00+00:00"},
        {"score": 6, "status": "applied", "first_seen": "2026-06-18T00:00:00+00:00"},
    ]
    suppressed = [
        {"score": 2, "first_seen": "2026-06-19T00:00:00+00:00"},
        {"score": 3, "first_seen": "2026-06-01T00:00:00+00:00"},
    ]
    a = score_analytics(matches, suppressed, now=now, window_days=7)
    assert a.histogram[8] == 1 and a.histogram[6] == 1   # notified scores
    assert a.histogram[2] == 1 and a.histogram[3] == 1   # suppressed scores
    assert sum(a.histogram) == 4
    assert a.total == 2                                  # notified only
    assert a.by_status == {"new": 1, "applied": 1}       # notified only
    assert a.recent == 2                                 # notified only
    assert a.suppressed == 2


def test_score_analytics_suppressed_defaults_empty():
    """Backwards-compatible: omitting the suppressed arg behaves like before."""
    now = datetime(2026, 6, 20, tzinfo=timezone.utc)
    a = score_analytics(
        [{"score": 8, "status": "new", "first_seen": "2026-06-19T00:00:00+00:00"}],
        now=now,
    )
    assert a.suppressed == 0
    assert sum(a.histogram) == 1


@pytest.fixture
def provider():
    conn = connect(":memory:")
    disc = SqliteDiscoveredSlugsStore(conn)
    disc.upsert_ok("greenhouse:ramp", last_posting_count=7)
    disc.upsert_failed("greenhouse:acme")
    seen = SqliteSeenJobsStore(conn)
    health = SqliteConnectorHealthStore(conn)
    yield OpsProvider(discovered=disc, seen=seen, health=health, log_group="/g", region="us-east-1")


def test_provider_health_surfaces_suppressed(provider):
    provider._health.record_dead("greenhouse:dead")
    provider._health.mark_suppressed("greenhouse:dead")
    h = provider.health()
    assert "greenhouse:dead" in h.suppressed
    # discovered-derived counts still present
    assert h.ok == 1 and h.failed == 1


def test_connector_health_default_has_empty_suppressed():
    assert connector_health([]).suppressed == []


def test_provider_health_and_analytics_from_dynamodb(provider):
    h = provider.health()
    assert h.ok == 1 and h.failed == 1
    a = provider.analytics()
    assert a.total == 0  # no notified rows seeded


def test_provider_analytics_includes_suppressed_rows(provider):
    """OpsProvider.analytics pulls suppressed rows from DynamoDB into the
    histogram while leaving total notified-only."""
    provider._seen.mark_suppressed("greenhouse:stripe:1", score=2)
    a = provider.analytics()
    assert a.suppressed == 1
    assert a.histogram[2] == 1
    assert a.total == 0   # still no notified rows seeded


def test_provider_cycles_fail_soft_when_cloudwatch_errors(provider, monkeypatch):
    import src.web.ops as ops_mod
    def boom(**kw):
        raise RuntimeError("no creds")
    monkeypatch.setattr(ops_mod, "load_pipeline_activity", boom)
    assert provider.cycles() is None  # swallowed → unavailable


def test_provider_health_fail_soft(provider, monkeypatch):
    monkeypatch.setattr(provider._discovered, "list_all", lambda: (_ for _ in ()).throw(RuntimeError("ddb down")))
    assert provider.health() is None


def test_provider_analytics_fail_soft(provider, monkeypatch):
    monkeypatch.setattr(provider._seen, "list_matches", lambda: (_ for _ in ()).throw(RuntimeError("ddb down")))
    assert provider.analytics() is None


def test_provider_health_suppressed_subsection_fail_soft(provider, monkeypatch):
    """If the health-store read raises, the suppressed sub-section degrades to []
    but the panel still renders with the discovered-derived counts intact."""
    monkeypatch.setattr(
        provider._health, "suppressed_names",
        lambda: (_ for _ in ()).throw(RuntimeError("ddb down")),
    )
    h = provider.health()
    assert h is not None
    assert h.suppressed == []
    assert h.ok == 1 and h.failed == 1
