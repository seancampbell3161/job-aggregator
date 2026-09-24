from datetime import datetime, timezone

from src.web.analytics import WeekBucket, matches_by_week


def test_matches_by_week_buckets_by_monday():
    now = datetime(2026, 6, 18, tzinfo=timezone.utc)  # Thursday; week Monday = 2026-06-15
    matches = [
        {"first_seen": "2026-06-16T10:00:00+00:00"},   # week of 06-15
        {"first_seen": "2026-06-15T00:00:00+00:00"},   # week of 06-15
        {"first_seen": "2026-06-09T00:00:00+00:00"},   # week of 06-08
    ]
    out = matches_by_week(matches, now=now, max_weeks=12)
    assert out == [WeekBucket("2026-06-08", 1), WeekBucket("2026-06-15", 2)]


def test_matches_by_week_trailing_cap_drops_far_past():
    now = datetime(2026, 6, 18, tzinfo=timezone.utc)   # week Monday = 2026-06-15
    matches = [
        {"first_seen": "2026-01-01T00:00:00+00:00"},   # far past — outside a 3-week window
        {"first_seen": "2026-06-16T00:00:00+00:00"},   # current week
    ]
    out = matches_by_week(matches, now=now, max_weeks=3)
    assert [b.label for b in out] == ["2026-06-01", "2026-06-08", "2026-06-15"]
    assert out[0].count == 0 and out[-1].count == 1


def test_matches_by_week_empty_input():
    assert matches_by_week([], now=datetime(2026, 6, 18, tzinfo=timezone.utc)) == []


def test_matches_by_week_skips_unparseable_first_seen():
    now = datetime(2026, 6, 18, tzinfo=timezone.utc)
    out = matches_by_week([{"first_seen": ""}, {"first_seen": "2026-06-16T00:00:00+00:00"}], now=now)
    assert [b.count for b in out] == [1]


from src.web.analytics import rank_by_ats, rank_by_company  # noqa: E402


def test_rank_by_company_counts_and_breaks_ties_by_name():
    matches = [
        {"company": "Stripe"}, {"company": "Stripe"}, {"company": "Ramp"},
        {"company": "Acme"}, {"company": ""},   # blank ignored
    ]
    assert rank_by_company(matches) == [("Stripe", 2), ("Acme", 1), ("Ramp", 1)]


def test_rank_by_ats_groups_on_provider_prefix():
    matches = [
        {"source": "greenhouse:stripe"}, {"source": "greenhouse:ramp"},
        {"source": "ashby:linear"}, {"source": "lever:plaid"},
        {"source": "soloslug"},   # no colon -> whole string is the key
        {"source": ""},           # blank ignored
    ]
    assert rank_by_ats(matches) == [
        ("greenhouse", 2), ("ashby", 1), ("lever", 1), ("soloslug", 1),
    ]


def test_rank_respects_top():
    matches = [{"company": c} for c in ["a", "a", "b", "b", "c", "c"]]
    assert rank_by_company(matches, top=2) == [("a", 2), ("b", 2)]


from src.web.analytics import gap_window  # noqa: E402


def test_gap_window_filters_and_counts_denominator():
    now = datetime(2026, 6, 18, tzinfo=timezone.utc)
    matches = [
        {"first_seen": "2026-06-17T00:00:00+00:00", "gaps": ["Kubernetes", "Kafka"]},
        {"first_seen": "2026-06-16T00:00:00+00:00", "gaps": ["Kubernetes"]},
        {"first_seen": "2026-06-10T00:00:00+00:00", "gaps": []},         # in window, clean
        {"first_seen": "2026-01-01T00:00:00+00:00", "gaps": ["Kafka"]},  # outside 30d window
    ]
    tally, total = gap_window(matches, now=now, window_days=30)
    assert total == 3                                   # three within 30d (Jan excluded)
    assert tally == [("Kubernetes", 2), ("Kafka", 1)]   # min_count=1 keeps the singleton


def test_gap_window_all_clean_returns_empty_tally():
    now = datetime(2026, 6, 18, tzinfo=timezone.utc)
    matches = [{"first_seen": "2026-06-17T00:00:00+00:00", "gaps": []}]
    tally, total = gap_window(matches, now=now)
    assert tally == [] and total == 1


def test_gap_window_missing_gaps_key_is_clean():
    now = datetime(2026, 6, 18, tzinfo=timezone.utc)
    tally, total = gap_window([{"first_seen": "2026-06-17T00:00:00+00:00"}], now=now)
    assert tally == [] and total == 1


import pytest  # noqa: E402

from src.models import NormalizedPosting  # noqa: E402
from src.sqlite_db import connect  # noqa: E402
from src.state_sqlite import SqliteSeenJobsStore  # noqa: E402
from src.web.analytics import MatchAnalytics  # noqa: E402


@pytest.fixture
def analytics_provider():
    seen = SqliteSeenJobsStore(connect(":memory:"))

    def seed(job_id, company, source, gaps):
        seen.claim_for_notify(
            job_id, score=8, rationale="why", gaps=gaps,
            posting=NormalizedPosting(
                job_id=job_id, title="Engineer", company=company,
                location_text="Remote (US)", location_tags=frozenset(),
                seniority="senior", stack=frozenset({"python"}),
                comp_min=180000, comp_max=220000, apply_url=f"https://apply/{job_id}",
                description="", posted_at=datetime(2026, 6, 16, tzinfo=timezone.utc),
                source=source,
            ),
        )

    seed("greenhouse:stripe:1", "Stripe", "greenhouse:stripe", ["Kubernetes", "Kafka"])
    seed("lever:ramp:2", "Ramp", "lever:ramp", [])
    yield MatchAnalytics(seen=seen)


def test_match_analytics_summary_shape(analytics_provider):
    a = analytics_provider.summary()
    assert a.total == 2
    assert ("Stripe", 1) in a.by_company and ("Ramp", 1) in a.by_company
    assert ("greenhouse", 1) in a.by_ats and ("lever", 1) in a.by_ats
    assert ("Kubernetes", 1) in a.gaps      # singleton surfaced (min_count=1)
    assert a.gaps_total == 2                 # both notified rows fall in the 30d window
    assert a.window_days == 30
    assert sum(b.count for b in a.by_week) == 2


def test_match_analytics_fail_soft(analytics_provider, monkeypatch):
    monkeypatch.setattr(
        analytics_provider._seen, "list_matches",
        lambda: (_ for _ in ()).throw(RuntimeError("ddb down")),
    )
    assert analytics_provider.summary() is None


def test_summary_includes_funnel_triage_and_rates(analytics_provider):
    a = analytics_provider.summary()
    counts = {n.id: n.count for n in a.funnel.nodes}
    assert counts["matches"] == 2 and counts["still_new"] == 2   # both seeds are status=new
    # The steps and standing bands cover only what triage let through, so with
    # both seeds still new they are empty and the triage band says it all.
    assert a.pipeline.live == 0
    assert a.pipeline.steps == [] and a.pipeline.standing == []
    assert a.pipeline.triage.total == 2
    assert [(s.id, s.count) for s in a.pipeline.triage.segments] == [("still_new", 2)]
    assert [r.label for r in a.rates] == ["Apply rate", "Interview rate", "Offer rate"]
    assert a.rates[0].num == 0 and a.rates[0].den == 2


def test_progress_strip_has_no_gaps_window_tile(tmp_path, monkeypatch):
    from tests.web.shell_helpers import client_for, make_app
    html = client_for(make_app(tmp_path, monkeypatch)).get("/analytics").text
    assert "gaps window" not in html
    assert "Matches found" in html


def test_no_stretch_skills_message_names_no_config_key(tmp_path, monkeypatch):
    """A newcomer never sees the config key `gap_analysis` — the empty state
    tells them where to turn the feature on in plain words."""
    from tests.web.shell_helpers import client_for, make_app, seed_match

    app = make_app(tmp_path, monkeypatch)
    seed_match(app, "j1")  # seeded with gaps=[] — no stretch skills recorded
    html = client_for(app).get("/analytics").text
    assert "gap_analysis" not in html
    assert "<code>" not in html
    assert "No stretch skills recorded yet." in html
    assert '<a href="/settings/llm#gaps">Turn on skill-gap analysis in Settings</a>' in html


def test_summary_unavailable_message_names_no_internal_table(tmp_path, monkeypatch):
    """When MatchAnalytics.summary() fails soft (returns None), the page must
    not mention the internal seen_jobs table."""
    from src.web.app import create_app
    from tests.auth_helpers import signed_in_client
    from tests.settings_helpers import WEB_TEST_SETTINGS, make_service

    class _UnavailableAnalytics:
        def summary(self, *, now=None):
            return None

    monkeypatch.setenv("JOB_AGG_SQLITE_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("JOB_AGG_TAILORED_DIR", str(tmp_path / "tailored"))
    app = create_app(
        service=make_service(WEB_TEST_SETTINGS), match_analytics=_UnavailableAnalytics()
    )
    html = signed_in_client(app).get("/analytics").text
    assert "seen_jobs" not in html
    assert "Match data is unavailable right now." in html
