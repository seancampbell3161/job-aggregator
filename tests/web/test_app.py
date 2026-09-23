import re
import sqlite3
import time
from datetime import datetime, timezone

import pytest

from src.models import NormalizedPosting
from src.sqlite_db import connect
from src.state_sqlite import SqliteDiscoveredSlugsStore, SqliteSeenJobsStore
from src.web.analytics import MatchAnalytics, MatchAnalyticsSummary, WeekBucket
from src.web.app import create_app
from src.web.funnel import build_funnel, build_pipeline, pipeline_rates
from src.web.repo import TriageRepo
from tests.auth_helpers import signed_in_client
from tests.settings_helpers import WEB_TEST_SETTINGS, configured_stores, make_service


@pytest.fixture
def client():
    conn = connect(":memory:")
    store = SqliteSeenJobsStore(conn)

    def seed(job_id, title, company, score, gaps, status=None):
        store.claim_for_notify(
            job_id, score=score, rationale=f"why {score}", gaps=gaps,
            posting=NormalizedPosting(
                job_id=job_id, title=title, company=company, location_text="Remote (US)",
                location_tags=frozenset(), seniority="senior", stack=frozenset({"python"}),
                comp_min=180000, comp_max=220000, apply_url=f"https://apply/{job_id}",
                description="", posted_at=datetime(2026, 6, 16, tzinfo=timezone.utc),
                source=company.lower(),
            ),
        )
        if status:
            store.set_status(job_id, status)

    seed("greenhouse:stripe:1", "Senior Backend Engineer", "Stripe", 8, ["Kafka", "Kubernetes"])
    seed("lever:ramp:2", "Staff Software Engineer", "Ramp", 7, [], status="applied")
    store.claim_for_notify(
        "adzuna:5001", score=6, rationale="why 6", gaps=[],
        posting=NormalizedPosting(
            job_id="adzuna:5001", title="Platform Engineer", company="TalentBridge",
            location_text="Austin, TX", location_tags=frozenset(), seniority="senior",
            stack=frozenset(), comp_min=None, comp_max=None,
            apply_url="https://talentbridge.example.com/jobs/42", description="",
            posted_at=datetime(2026, 7, 17, tzinfo=timezone.utc), source="adzuna",
        ),
    )
    from src.web.ops import OpsProvider
    disc = SqliteDiscoveredSlugsStore(conn)
    disc.upsert_ok("greenhouse:ramp", last_posting_count=7)
    disc.upsert_failed("greenhouse:acme")
    ops = OpsProvider(discovered=disc, seen=store)
    app = create_app(
        repo=TriageRepo(store), ops=ops, match_analytics=MatchAnalytics(seen=store),
        service=make_service(WEB_TEST_SETTINGS),
    )
    yield signed_in_client(app)


def test_inbox_shell_renders(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Job Aggregator" in r.text
    assert 'id="filters"' in r.text
    assert 'id="list"' in r.text
    assert 'id="detail"' in r.text
    assert "/static/htmx.min.js" in r.text


def test_jobs_list_renders_rows(client):
    r = client.get("/jobs")
    assert r.status_code == 200
    assert "Senior Backend Engineer" in r.text
    assert "Stripe" in r.text


def test_jobs_list_status_filter(client):
    # only the applied job (Ramp); Stripe is "new" and excluded
    r = client.get("/jobs", params=[("status", "applied")])
    assert "Ramp" in r.text
    assert "Stripe" not in r.text


def test_jobs_list_min_score_filter(client):
    r = client.get("/jobs", params={"min_score": 8})
    assert "Stripe" in r.text     # score 8
    assert "Ramp" not in r.text   # score 7


def test_jobs_list_has_gaps_filter(client):
    r = client.get("/jobs", params={"has_gaps": "true"})
    assert "Stripe" in r.text     # has Kafka/Kubernetes
    assert "Ramp" not in r.text   # no gaps


def test_jobs_list_workplace_filter(client):
    # Stripe and Ramp are location_text="Remote (US)" → "remote"; the adzuna
    # seed row (TalentBridge) is "Austin, TX" → "onsite".
    r = client.get("/jobs", params=[("workplace", "remote")])
    assert "Stripe" in r.text and "Ramp" in r.text
    assert "TalentBridge" not in r.text          # onsite row excluded by remote-only
    # filtering to onsite-only excludes both remote rows, keeps the onsite row
    r = client.get("/jobs", params=[("workplace", "onsite")])
    assert "Stripe" not in r.text and "Ramp" not in r.text
    assert "TalentBridge" in r.text
    # no workplace param → unfiltered (all rows), same as every box checked
    r = client.get("/jobs")
    assert "Stripe" in r.text and "Ramp" in r.text


def test_jobs_list_search(client):
    r = client.get("/jobs", params={"q": "ramp"})
    assert "Ramp" in r.text
    assert "Stripe" not in r.text


def test_jobs_list_empty_state(client):
    r = client.get("/jobs", params={"min_score": 10})  # nothing scores that high
    assert r.status_code == 200
    # matches exist (Stripe/Ramp/TalentBridge) but the min_score floor hides
    # them all, so this is the "filtered" empty state, not "nothing yet".
    assert "Nothing matches these filters." in r.text


def test_jobs_list_blank_min_score_is_no_filter(client):
    # the inbox number input serializes an empty box as min_score="" — must be
    # treated as "no floor", not rejected with 422
    r = client.get("/jobs", params={"min_score": ""})
    assert r.status_code == 200
    assert "Stripe" in r.text and "Ramp" in r.text


def test_jobs_list_form_default_params_load(client):
    # mirror exactly what the inbox form's #list hx-include sends on initial load:
    # all status boxes except dismissed checked, blank q, blank min_score, sort=score.
    # Regression guard for the form-to-endpoint contract (caught the min_score 422).
    r = client.get("/jobs", params=[
        ("q", ""),
        ("status", "new"), ("status", "interested"),
        ("status", "applied"), ("status", "interviewing"),
        ("min_score", ""), ("has_gaps", "false"), ("sort", "score"),
    ])
    assert r.status_code == 200
    assert "Stripe" in r.text   # new
    assert "Ramp" in r.text     # applied


def test_jobs_list_no_pager_when_single_page(client):
    # default page_size (10) easily holds the 3 seeded rows → no pager rendered
    r = client.get("/jobs")
    assert r.status_code == 200
    assert 'class="pager"' not in r.text


def test_jobs_list_paginates(client):
    client.app.state.page_size = 1
    # sort=score → Stripe (8) on page 1, Ramp (7) on page 2, TalentBridge (6) on page 3
    p1 = client.get("/jobs", params={"sort": "score"})
    assert "Stripe" in p1.text and "Ramp" not in p1.text
    assert 'class="pager"' in p1.text
    assert "page 1/3" in p1.text
    assert 'hx-include="#filters"' in p1.text  # pager preserves active filters
    p2 = client.get("/jobs", params={"sort": "score", "page": 2})
    assert "Ramp" in p2.text and "Stripe" not in p2.text
    assert "page 2/3" in p2.text


def test_jobs_list_page_out_of_range_clamps(client):
    client.app.state.page_size = 1
    # page far past the end clamps to the last page rather than 422-ing or 404-ing
    r = client.get("/jobs", params={"sort": "score", "page": 99})
    assert r.status_code == 200
    assert "page 3/3" in r.text
    assert "TalentBridge" in r.text  # last page holds the score-6 row


def test_inbox_offers_page_size_selector(client):
    # the per-page control lives in the (static) filter form, defaulting to 10
    r = client.get("/")
    assert 'name="page_size"' in r.text
    assert '<option value="10" selected>' in r.text
    assert 'value="25"' in r.text and 'value="50"' in r.text


def test_jobs_page_size_param_overrides_default(client):
    client.app.state.page_size = 1  # would paginate on its own …
    # … but an in-range page_size query param wins: 25 holds both rows, no pager
    r = client.get("/jobs", params={"sort": "score", "page_size": 25})
    assert 'class="pager"' not in r.text
    assert "Stripe" in r.text and "Ramp" in r.text


def test_jobs_page_size_out_of_set_falls_back_to_default(client):
    client.app.state.page_size = 25
    # a hand-crafted value not in {10,25,50} must not force a slice — fall back
    r = client.get("/jobs", params={"page_size": 1})   # 1 is not an offered size
    assert 'class="pager"' not in r.text               # 25 holds both rows, no pager
    assert "Stripe" in r.text and "Ramp" in r.text


def test_bulk_status_dismisses_many(client):
    # both rows selected → dismissed in one POST; the re-rendered list (default
    # filters exclude 'dismissed', but TalentBridge remains as new)
    r = client.post(
        "/bulk-status",
        data={
            "ids": ["greenhouse:stripe:1", "lever:ramp:2"],
            "to_status": "dismissed",
            "status": ["new", "interested", "applied", "interviewing"],
        },
    )
    assert r.status_code == 200
    assert "TalentBridge" in r.text  # the adzuna row (new status) remains
    assert "Stripe" not in r.text and "Ramp" not in r.text  # these two were dismissed
    # both are now dismissed and only surface under the dismissed filter
    dismissed = client.get("/jobs", params=[("status", "dismissed")])
    assert "Stripe" in dismissed.text and "Ramp" in dismissed.text


def test_bulk_status_rejects_invalid_target(client):
    r = client.post(
        "/bulk-status", data={"ids": ["greenhouse:stripe:1"], "to_status": "bogus"}
    )
    assert r.status_code == 400


def test_bulk_status_no_ids_is_noop(client):
    # applying with nothing selected changes nothing and still renders the list
    r = client.post("/bulk-status", data={"to_status": "dismissed"})
    assert r.status_code == 200
    still = client.get("/jobs")
    assert "Stripe" in still.text and "Ramp" in still.text


def test_jobs_list_rows_have_bulk_checkboxes(client):
    r = client.get("/jobs")
    assert 'name="ids"' in r.text          # per-row select checkbox
    assert 'class="bulk-bar"' in r.text     # bulk action bar
    assert 'hx-post="/bulk-status"' in r.text


def test_detail_renders_full_match(client):
    r = client.get("/detail", params={"id": "greenhouse:stripe:1"})
    assert r.status_code == 200
    assert "Senior Backend Engineer" in r.text
    assert "why 8" in r.text                # rationale
    assert "Kafka" in r.text and "Kubernetes" in r.text  # gaps
    assert "$180k–$220k" in r.text          # comp_display
    assert 'href="https://apply/greenhouse:stripe:1"' in r.text  # apply link
    assert "/status?id=" in r.text          # status buttons present
    assert 'class="btn primary"' in r.text  # current status (new) button marked active


def test_detail_missing_returns_expired_partial(client):
    r = client.get("/detail", params={"id": "nope:0:0"})
    assert r.status_code == 200
    assert "expired" in r.text.lower()
    assert r.headers.get("HX-Trigger") == "refreshList"


def test_set_status_updates_and_returns_detail(client):
    r = client.post("/status", params={"id": "greenhouse:stripe:1", "status": "interested"})
    assert r.status_code == 200
    assert r.headers.get("HX-Trigger") == "refreshList"
    # the returned detail marks 'Interested' as the active (primary) button — not
    # merely that the word appears (all five buttons always render)
    assert re.search(r'class="btn primary"[^>]*>\s*Interested', r.text)
    # change persisted: re-fetching the detail still shows Interested active
    detail = client.get("/detail", params={"id": "greenhouse:stripe:1"}).text
    assert re.search(r'class="btn primary"[^>]*>\s*Interested', detail)
    # and the job now appears under the interested filter
    follow = client.get("/jobs", params=[("status", "interested")])
    assert "Stripe" in follow.text


def test_set_status_rejects_invalid(client):
    r = client.post("/status", params={"id": "greenhouse:stripe:1", "status": "bogus"})
    assert r.status_code == 400


def test_set_status_missing_row_returns_expired(client):
    r = client.post("/status", params={"id": "nope:0:0", "status": "applied"})
    assert r.status_code == 200
    assert "expired" in r.text.lower()
    assert r.headers.get("HX-Trigger") == "refreshList"


@pytest.fixture
def status_suggestion_client(tmp_path, monkeypatch):
    """Minimal sqlite-backed app. Mirrors tests/web/test_board.py's
    board_client fixture."""
    from src.sqlite_db import connect

    monkeypatch.setenv("JOB_AGG_TAILORED_DIR", str(tmp_path / "tailored"))
    monkeypatch.delenv("JOB_AGG_OPS_NTFY_TOPIC_URL", raising=False)
    monkeypatch.delenv("JOB_AGG_OPS_DISCORD_WEBHOOK_URL", raising=False)
    conn = connect(":memory:")
    stores = configured_stores(conn)
    seen = stores.seen
    seen.claim_for_notify(
        "greenhouse:acme:1", score=7, rationale="Good fit",
        posting=NormalizedPosting(
            job_id="greenhouse:acme:1", title="Senior Engineer", company="Acme",
            location_text="Remote (US)", location_tags=frozenset({"remote", "us"}),
            seniority="senior", stack=frozenset({"python"}), comp_min=None, comp_max=None,
            apply_url="https://apply/greenhouse:acme:1", description="",
            posted_at=datetime(2026, 6, 16, tzinfo=timezone.utc), source="greenhouse:acme",
        ),
    )
    app = create_app(repo=TriageRepo(seen), stores=stores)
    return signed_in_client(app), seen


def test_set_status_clears_email_suggestion(status_suggestion_client):
    client, store = status_suggestion_client
    store.update_email_suggestion("greenhouse:acme:1", suggestion={
        "suggested_status": "rejected", "subject": "Update on your application",
        "from": "no-reply@us.greenhouse-mail.io",
        "date": "2026-07-02T14:03:11+00:00",
        "matched_phrase": "unfortunately", "message_id": "<r1@ats>",
    })
    r = client.post("/status", params={"id": "greenhouse:acme:1", "status": "rejected"})
    assert r.status_code == 200
    assert store.get_match("greenhouse:acme:1")["email_suggestion"] is None


def test_main_module_builds_app(monkeypatch, tmp_path):
    """`python -m src.web` path: main() must build an app and start the server.
    We stub uvicorn.run and the startup check so nothing actually binds a port.

    Also asserts the triage UI starts WITHOUT notification secrets set — it never
    notifies, so it must not require ntfy/Discord env vars (regression guard for
    the old YAML-loader coupling)."""
    import src.web.__main__ as entry

    monkeypatch.delenv("JOB_AGG_NTFY_TOPIC_URL", raising=False)
    monkeypatch.delenv("JOB_AGG_DISCORD_WEBHOOK_URL", raising=False)
    monkeypatch.setenv("JOB_AGG_SQLITE_PATH", str(tmp_path / "t.db"))

    calls = {}
    monkeypatch.setattr(entry.uvicorn, "run", lambda app, **kw: calls.update(kw, app=app))

    monkeypatch.setattr(entry, "_startup_repo_ok", lambda app: True)
    rc = entry.main()
    assert rc == 0
    assert calls["host"] == "127.0.0.1"
    assert calls["app"] is not None


def test_main_module_exits_1_when_repo_unreachable(monkeypatch, tmp_path):
    """If the backend is unreachable, main() returns exit code 1 and never binds a
    port (the friendly-startup-failure branch)."""
    import src.web.__main__ as entry

    monkeypatch.setenv("JOB_AGG_SQLITE_PATH", str(tmp_path / "t.db"))

    ran = {"called": False}
    monkeypatch.setattr(entry, "_startup_repo_ok", lambda app: False)
    monkeypatch.setattr(entry.uvicorn, "run", lambda *a, **k: ran.update(called=True))
    assert entry.main() == 1
    assert ran["called"] is False


def test_nav_links_present_on_triage_page(client):
    r = client.get("/")
    assert 'href="/pipeline"' in r.text
    assert 'href="/"' in r.text


def test_pipeline_page_renders_strip_and_panels(client):
    r = client.get("/pipeline")
    assert r.status_code == 200
    assert "greenhouse:acme" in r.text          # the failing connector is listed
    assert "Connector health" in r.text
    assert "Match &amp; score" in r.text or "Match & score" in r.text
    assert 'hx-get="/pipeline/cycles"' in r.text
    assert 'hx-trigger="load, every 60s"' in r.text


def test_pipeline_page_health_unavailable_is_soft(client, monkeypatch):
    monkeypatch.setattr(client.app.state.ops, "health", lambda: None)
    r = client.get("/pipeline")
    assert r.status_code == 200
    assert "unavailable" in r.text.lower()


def test_pipeline_page_analytics_unavailable_is_soft(client, monkeypatch):
    monkeypatch.setattr(client.app.state.ops, "analytics", lambda: None)
    r = client.get("/pipeline")
    assert r.status_code == 200
    assert "unavailable" in r.text.lower()


def test_pipeline_cycles_renders_with_activity(client, monkeypatch):
    from src.web.pipeline_activity import LastCycle, PipelineActivity, Tally, TierStats
    now_ms = int(time.time() * 1000)
    activity = PipelineActivity(
        window_days=7,
        tiers=[TierStats("ats", 100, 1480.0, 6.0, 1.0, 4200.0)],
        failures_by_type=[
            Tally("DataDome", 12, now_ms - 3_600_000),         # 1h ago — active
            Tally("PoolTimeout", 4, now_ms - 3 * 86_400_000),  # 3d ago — dimmed
        ],
        failures_by_connector=[Tally("ashby:vercel", 12, now_ms - 3_600_000)],
        failures_total=16,
        last_cycle=LastCycle(ts_ms=1_000_000, ok=True),
    )
    monkeypatch.setattr(client.app.state.ops, "cycles", lambda: activity)
    r = client.get("/pipeline/cycles")
    assert r.status_code == 200
    assert "ats" in r.text and "1480" in r.text           # per-tier averages
    assert "DataDome" in r.text and "1h ago" in r.text    # recency rendered inline
    assert "ashby:vercel" in r.text
    assert 'class="bad dim"' in r.text                    # 3d-old tally is dimmed
    assert 'id="last-cycle"' in r.text and "hx-swap-oob" in r.text


def test_pipeline_cycles_oob_updates_last_success(client, monkeypatch):
    from src.web.pipeline_activity import LastCycle, PipelineActivity
    activity = PipelineActivity(
        window_days=7, tiers=[], failures_by_type=[], failures_by_connector=[],
        failures_total=0, last_cycle=LastCycle(ts_ms=1_000_000, ok=True),
    )
    monkeypatch.setattr(client.app.state.ops, "cycles", lambda: activity)
    now_ms = int(time.time() * 1000)
    monkeypatch.setattr(client.app.state.ops, "last_success", lambda: now_ms - 120_000)
    r = client.get("/pipeline/cycles")
    assert 'id="last-success"' in r.text and "hx-swap-oob" in r.text
    assert "2m ago" in r.text


def test_pipeline_cycles_renders_recent_cycles_table(client, monkeypatch):
    from src.web.pipeline_activity import CycleRow, LastCycle, PipelineActivity
    now_ms = int(time.time() * 1000)
    activity = PipelineActivity(
        window_days=7, tiers=[], failures_by_type=[], failures_by_connector=[],
        failures_total=0, last_cycle=LastCycle(ts_ms=now_ms, ok=True),
        recent=[
            CycleRow(ts_ms=now_ms - 60_000, tier="ats", fetched=1480, new_count=3,
                     matched=2, notified=1, duration_ms=4200, ok=True, degraded=False,
                     failures=[]),
            CycleRow(ts_ms=now_ms - 660_000, tier="slow", fetched=200, new_count=None,
                     matched=0, notified=0, duration_ms=900, ok=False, degraded=True,
                     failures=[("lever:y", "HTTP500")]),
        ],
    )
    monkeypatch.setattr(client.app.state.ops, "cycles", lambda: activity)
    r = client.get("/pipeline/cycles")
    assert r.status_code == 200
    assert "recent cycles" in r.text.lower()
    assert "<details open>" in r.text
    assert "1480" in r.text and "1m ago" in r.text     # newest row rendered
    assert "lever:y:HTTP500" in r.text                 # failure detail in title attr
    assert "LLM⚠" in r.text                            # degraded marker on the bad row
    assert "–" in r.text                               # None new_count placeholder


def test_pipeline_cycles_hides_recent_table_when_empty(client, monkeypatch):
    from src.web.pipeline_activity import LastCycle, PipelineActivity
    activity = PipelineActivity(
        window_days=7, tiers=[], failures_by_type=[], failures_by_connector=[],
        failures_total=0, last_cycle=LastCycle(ts_ms=1_000_000, ok=True),
    )
    monkeypatch.setattr(client.app.state.ops, "cycles", lambda: activity)
    r = client.get("/pipeline/cycles")
    assert "recent cycles" not in r.text.lower()   # no telemetry → no section


def test_pipeline_cycles_unavailable_is_soft(client, monkeypatch):
    monkeypatch.setattr(client.app.state.ops, "cycles", lambda: None)
    r = client.get("/pipeline/cycles")
    assert r.status_code == 200
    assert "unavailable" in r.text.lower()
    # the strip's last-cycle chip must still resolve (out of the "…" loading state)
    assert 'id="last-cycle"' in r.text and "hx-swap-oob" in r.text
    # the tier-freshness strip must also resolve out of its loading placeholder
    assert 'id="tier-freshness"' in r.text


def test_pipeline_cycles_renders_per_tier_freshness(client, monkeypatch):
    from src.web.pipeline_activity import LastCycle, PipelineActivity, TierHeartbeat, TierStats

    now_ms = int(time.time() * 1000)
    activity = PipelineActivity(
        window_days=7,
        tiers=[
            TierStats("ats", 100, 1480.0, 6.0, 1.0, 4200.0),
            TierStats("slow", 50, 200.0, 2.0, 0.5, 900.0),
        ],
        failures_by_type=[], failures_by_connector=[], failures_total=0,
        last_cycle=LastCycle(ts_ms=now_ms, ok=True),
        tier_heartbeats=[
            # ats: 1-min cadence but last cycle 6h ago → stalled (the silent-death case)
            TierHeartbeat("ats", LastCycle(ts_ms=now_ms - 6 * 3600_000, ok=True), median_gap_ms=60_000),
            # slow: hourly cadence, ticked just now → fresh
            TierHeartbeat("slow", LastCycle(ts_ms=now_ms, ok=True), median_gap_ms=3600_000),
        ],
    )
    monkeypatch.setattr(client.app.state.ops, "cycles", lambda: activity)
    r = client.get("/pipeline/cycles")
    assert r.status_code == 200
    # freshness strip is OOB-swapped into the header placeholder
    assert 'id="tier-freshness"' in r.text and "hx-swap-oob" in r.text
    assert "tier-chip" in r.text
    assert "stalled" in r.text          # ats flagged as stalled
    # freshness moved OUT of the throughput table — no "last" column header remains
    assert "<th>last</th>" not in r.text


def test_pipeline_cycles_failed_last_cycle_shows_warning(client, monkeypatch):
    from src.web.pipeline_activity import LastCycle, PipelineActivity
    activity = PipelineActivity(
        window_days=7, tiers=[], failures_by_type=[], failures_by_connector=[],
        failures_total=0, last_cycle=LastCycle(ts_ms=1_000_000, ok=False),
    )
    monkeypatch.setattr(client.app.state.ops, "cycles", lambda: activity)
    r = client.get("/pipeline/cycles")
    assert r.status_code == 200
    assert "⚠" in r.text  # a failed last cycle flags the chip


def test_analytics_page_renders_panels(client):
    r = client.get("/analytics")
    assert r.status_code == 200
    assert "Matches over time" in r.text
    assert "Where matches come from" in r.text
    assert "Stretch skills" in r.text
    assert "Stripe" in r.text          # a company in the breakdown
    assert "Kubernetes" in r.text      # a stretch-skill from the seeded gaps
    assert 'class="b s-vol"' in r.text  # the matches-over-time histogram rendered a bar


def test_analytics_unavailable_is_soft(client, monkeypatch):
    monkeypatch.setattr(client.app.state.match_analytics, "summary", lambda: None)
    r = client.get("/analytics")
    assert r.status_code == 200
    assert "unavailable" in r.text.lower()


def test_nav_has_analytics_link(client):
    r = client.get("/")
    assert 'href="/analytics"' in r.text


def test_analytics_empty_data_state(client, monkeypatch):
    # With no matches, the timeline and breakdown panels render their empty hint.
    funnel = build_funnel([])
    empty = MatchAnalyticsSummary(
        total=0, by_week=[], by_company=[], by_ats=[], gaps=[], gaps_total=0, window_days=30,
        funnel=funnel, pipeline=build_pipeline([]), rates=pipeline_rates([]),
    )
    monkeypatch.setattr(client.app.state.match_analytics, "summary", lambda: empty)
    r = client.get("/analytics")
    assert r.status_code == 200
    assert "No matches yet." in r.text


def test_board_page_shows_applied_card(client):
    r = client.get("/board")
    assert r.status_code == 200
    assert "Ramp" in r.text
    assert "Applied" in r.text

def test_board_advance_moves_card(client):
    r = client.post("/board/advance", params={"id": "lever:ramp:2", "status": "interviewing"})
    assert r.status_code == 200
    text = r.text
    # response is the re-rendered columns partial; the card now sits under Interviewing
    assert "Ramp" in text
    # Positional proof that Ramp landed in the Interviewing column.
    # The fixture leaves Interested and Applied empty (no cards, no action buttons),
    # so the first occurrence of "Interviewing" is the column <h2> header and the
    # first occurrence of "Offer" is the next column header — Ramp must fall between them.
    assert text.index("Interviewing") < text.index("Ramp") < text.index("Offer")

def test_board_advance_rejects_invalid_status(client):
    r = client.post("/board/advance", params={"id": "lever:ramp:2", "status": "bogus"})
    assert r.status_code == 400

def test_board_in_nav(client):
    assert 'href="/board"' in client.get("/").text


def test_detail_offers_new_outcome_statuses(client):
    # the detail pane is fetched for a seeded row; it should now offer offer/rejected/ghosted
    r = client.get("/detail", params={"id": "lever:ramp:2"})
    assert r.status_code == 200
    for s in ("Offer", "Rejected", "Ghosted"):
        assert s in r.text


def test_analytics_no_gaps_shows_enable_hint(client, monkeypatch):
    # Matches exist but none have recorded gaps (the production state while
    # gap_analysis is off): the stretch-skills panel shows the enable hint while
    # the other panels render their data.
    _two = [
        {"status": "new", "history": []},
        {"status": "applied", "history": [{"status": "applied", "at": "2026-07-01T00:00:00+00:00"}]},
    ]
    funnel = build_funnel(_two)
    summary = MatchAnalyticsSummary(
        total=2, by_week=[WeekBucket("2026-06-15", 2)],
        by_company=[("Stripe", 2)], by_ats=[("greenhouse", 2)],
        gaps=[], gaps_total=2, window_days=30,
        funnel=funnel, pipeline=build_pipeline(_two), rates=pipeline_rates(_two),
    )
    monkeypatch.setattr(client.app.state.match_analytics, "summary", lambda: summary)
    r = client.get("/analytics")
    assert r.status_code == 200
    assert "gap_analysis" in r.text          # the enable hint is shown
    assert "No matches yet." not in r.text   # matches exist; only the gaps panel is empty


def test_analytics_page_renders_pipeline_panel(client):
    r = client.get("/analytics")
    assert r.status_code == 200
    assert "Pipeline" in r.text
    assert "Triage" in r.text                  # the triage band
    assert "How far they got" in r.text        # the progression funnel
    assert "stand now" in r.text               # the current-standing band
    assert "table view" in r.text              # stage-level flows, accessible twin
    assert "Interview rate" in r.text and "Apply rate" in r.text
    assert "1 of 3 matches" in r.text          # apply-rate caption (Ramp seed is applied)


def test_jobs_list_always_shows_total(client):
    r = client.get("/jobs")
    assert 'id="list-total"' in r.text and "3 matches" in r.text
    r = client.get("/jobs", params=[("status", "applied")])
    assert ">1 match</span>" in r.text   # singular form, no trailing 'es'


def test_new_count_counts_rows_after_watermark(client):
    r = client.get("/jobs/new-count", params={"since": "2020-01-01T00:00:00+00:00"})
    assert r.status_code == 200
    assert ">3 new</span>" in r.text


def test_new_count_empty_when_nothing_new(client):
    r = client.get("/jobs/new-count", params={"since": "2999-01-01T00:00:00+00:00"})
    assert r.status_code == 200 and r.text == ""


def test_new_count_accepts_js_toISOString_zulu(client):
    r = client.get("/jobs/new-count", params={"since": "2020-01-01T00:00:00.000Z"})
    assert ">3 new</span>" in r.text


def test_new_count_garbage_or_missing_since_fails_soft(client):
    assert client.get("/jobs/new-count", params={"since": "not-a-date"}).text == ""
    assert client.get("/jobs/new-count").text == ""


def test_inbox_shell_has_refresh_badge_and_persistence(client):
    r = client.get("/")
    assert 'id="refresh"' in r.text
    assert 'id="new-badge"' in r.text
    assert "/jobs/new-count" in r.text          # 60s poller wired
    assert "triage.filters.v1" in r.text        # localStorage persistence script
    assert 'id="since"' in r.text               # watermark input


def test_detail_shows_adzuna_attribution(client):
    r = client.get("/detail", params={"id": "adzuna:5001"})
    assert r.status_code == 200
    assert '<a href="https://www.adzuna.com"' in r.text
    assert "Jobs by Adzuna" in r.text


def test_detail_no_attribution_for_other_sources(client):
    r = client.get("/detail", params={"id": "greenhouse:stripe:1"})
    assert "Jobs by Adzuna" not in r.text


def test_jobs_list_shows_adzuna_attribution_on_row(client):
    r = client.get("/jobs")
    assert "Jobs by Adzuna" in r.text
    assert 'href="https://www.adzuna.com"' in r.text


def test_jobs_list_attribution_only_on_adzuna_rows(client):
    r = client.get("/jobs", params={"status": "applied"})  # only the Ramp row (lever source)
    assert "Jobs by Adzuna" not in r.text


def test_startup_check_reads_the_login_tables(monkeypatch, tmp_path):
    import src.web.__main__ as entry

    monkeypatch.setenv("JOB_AGG_SQLITE_PATH", str(tmp_path / "t.db"))
    app = entry.create_app()
    assert entry._startup_repo_ok(app) is True

    class Broken:
        def has_password(self):
            raise sqlite3.OperationalError("no such table: auth_credential")

    app.state.auth = Broken()
    assert entry._startup_repo_ok(app) is False
