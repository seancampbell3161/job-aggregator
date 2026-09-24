from datetime import datetime, timezone

import pytest
from freezegun import freeze_time

from src.models import NormalizedPosting
from src.sqlite_db import connect
from src.web.app import create_app
from src.web.board import ACTIVE_COLUMNS, Board, BoardProvider
from src.web.repo import TriageMatch, TriageRepo
from tests.auth_helpers import signed_in_client
from tests.settings_helpers import configured_stores


def _m(job_id, status, *, history=None, score=5):
    return TriageMatch(
        job_id=job_id, title="Eng", company="Foo", location_text="Remote",
        comp_min=None, comp_max=None, apply_url="https://x", source="greenhouse:foo",
        posted_at=None, score=score, rationale="r", gaps=[],
        first_seen="2026-06-01T00:00:00+00:00", status=status, history=history or [],
    )


class _StubRepo:
    def __init__(self, matches):
        self._matches = matches
    def list(self, **_):
        return list(self._matches)


class _RaisingRepo:
    def list(self, **_):
        raise RuntimeError("ddb down")


def test_active_statuses_bucket_into_columns():
    b = BoardProvider(_StubRepo([
        _m("a", "interested"), _m("b", "applied"),
        _m("c", "interviewing"), _m("d", "offer"),
    ])).board()
    assert [b.columns[c][0].job_id for c in ACTIVE_COLUMNS] == ["a", "b", "c", "d"]
    assert b.archive == []

def test_new_is_excluded_entirely():
    b = BoardProvider(_StubRepo([_m("a", "new")])).board()
    assert all(b.columns[c] == [] for c in ACTIVE_COLUMNS)
    assert b.archive == []

def test_rejected_and_ghosted_go_to_archive():
    b = BoardProvider(_StubRepo([_m("a", "rejected"), _m("b", "ghosted")])).board()
    assert {m.job_id for m in b.archive} == {"a", "b"}

def test_pursued_dismissed_archived_but_inbox_swipe_excluded():
    pursued = _m("a", "dismissed", history=[
        {"status": "interested", "at": "2026-06-01T00:00:00+00:00"},
        {"status": "dismissed", "at": "2026-06-05T00:00:00+00:00"},
    ])
    swipe = _m("b", "dismissed", history=[{"status": "dismissed", "at": "2026-06-05T00:00:00+00:00"}])
    legacy = _m("c", "dismissed", history=[])  # pre-migration, no history
    b = BoardProvider(_StubRepo([pursued, swipe, legacy])).board()
    assert [m.job_id for m in b.archive] == ["a"]

@freeze_time("2026-06-20T00:00:00Z")
def test_columns_sorted_stalest_first():
    older = _m("old", "applied", history=[{"status": "applied", "at": "2026-06-01T00:00:00+00:00"}])
    newer = _m("new", "applied", history=[{"status": "applied", "at": "2026-06-18T00:00:00+00:00"}])
    b = BoardProvider(_StubRepo([newer, older])).board()
    assert [m.job_id for m in b.columns["applied"]] == ["old", "new"]

def test_fail_soft_on_repo_error():
    b = BoardProvider(_RaisingRepo()).board()
    assert b.error is True
    assert all(b.columns[c] == [] for c in ACTIVE_COLUMNS)
    assert b.archive == []


def _posting(job_id, title, company="Acme"):
    return NormalizedPosting(
        job_id=job_id, title=title, company=company, location_text="Remote (US)",
        location_tags=frozenset({"remote", "us"}), seniority="mid",
        stack=frozenset({"python"}), comp_min=None, comp_max=None,
        apply_url=f"https://apply/{job_id}", description="Ship Python services.",
        posted_at=datetime(2026, 7, 1, tzinfo=timezone.utc), source="greenhouse:acme",
    )


@pytest.fixture
def board_client(tmp_path, monkeypatch):
    monkeypatch.setenv("JOB_AGG_TAILORED_DIR", str(tmp_path / "tailored"))
    monkeypatch.delenv("JOB_AGG_OPS_NTFY_TOPIC_URL", raising=False)
    monkeypatch.delenv("JOB_AGG_OPS_DISCORD_WEBHOOK_URL", raising=False)
    conn = connect(":memory:")
    stores = configured_stores(conn)
    seen = stores.seen
    # Seed an applied card
    seen.claim_for_notify(
        "greenhouse:acme:applied-card", score=7, rationale="Good fit",
        posting=_posting("greenhouse:acme:applied-card", "Senior Engineer"),
    )
    seen.set_status("greenhouse:acme:applied-card", "applied")

    app = create_app(repo=TriageRepo(seen), stores=stores)
    return signed_in_client(app), seen


def test_board_card_renders_posting_closed_badge(board_client):
    client, store = board_client
    # Seed an applied card and flag it as closed
    store.update_closed_check("greenhouse:acme:applied-card", misses=2, closed_at="2026-06-29T04:30:00+00:00")
    r = client.get("/board")
    assert r.status_code == 200
    assert "posting closed" in r.text


def _suggest(store, job_id, status="rejected", msg_id="<r1@ats>"):
    store.update_email_suggestion(job_id, suggestion={
        "suggested_status": status, "subject": "Update on your application",
        "from": "no-reply@us.greenhouse-mail.io",
        "date": "2026-07-02T14:03:11+00:00",
        "matched_phrase": "unfortunately", "message_id": msg_id,
    })


CARD = "greenhouse:acme:applied-card"


def test_board_renders_suggestion_badge_with_confirm_and_dismiss(board_client):
    client, store = board_client
    _suggest(store, CARD)
    r = client.get("/board")
    assert "looks rejected" in r.text
    assert "/board/dismiss-suggestion" in r.text


def test_advance_clears_suggestion(board_client):
    client, store = board_client
    _suggest(store, CARD)
    r = client.post(f"/board/advance?id={CARD}&status=rejected")
    assert r.status_code == 200
    assert store.get_match(CARD)["email_suggestion"] is None
    assert store.get_match(CARD)["dismissed_suggestions"] == []  # confirm ≠ dismiss


def test_dismiss_clears_and_records(board_client):
    client, store = board_client
    _suggest(store, CARD)
    r = client.post(f"/board/dismiss-suggestion?id={CARD}")
    assert r.status_code == 200
    m = store.get_match(CARD)
    assert m["email_suggestion"] is None
    assert m["dismissed_suggestions"] == ["<r1@ats>"]


# ---- manual add ------------------------------------------------------------

ADD = {
    "company": "Northwind Labs", "title": "Staff Platform Engineer",
    "status": "interested", "apply_url": "https://northwind.example/jobs/42",
    "location_text": "Remote (US)", "comp_min": "210k", "comp_max": "$250,000",
    "channel": "LinkedIn recruiter", "description": "Own the platform team.",
}


def _added(store):
    return next(m for m in store.list_matches() if m["job_id"].startswith("manual:"))


def test_add_job_redirects_and_lands_on_the_board(board_client):
    client, store = board_client
    r = client.post("/board/add", data=ADD, follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, "/board")
    m = _added(store)
    assert (m["company"], m["title"], m["status"]) == (
        "Northwind Labs", "Staff Platform Engineer", "interested")
    assert m["comp_min"] == 210000 and m["comp_max"] == 250000   # '210k' / '$250,000'
    assert m["source"] == "manual:linkedin-recruiter"
    body = client.get("/board").text
    assert "Northwind Labs" in body and "manual" in body


def test_added_job_is_counted_in_analytics(board_client):
    """The point of writing it as a normal notified match: every analytics
    section picks it up with no special-casing."""
    client, store = board_client
    before = client.get("/analytics").text
    assert "<td>manual</td>" not in before
    # The Sankey starts after triage, so its first column is "In pipeline".
    assert "<td>In pipeline</td><td>Interested</td><td>1</td>" in before  # the seeded card only
    client.post("/board/add", data=ADD)
    after = client.get("/analytics").text
    assert "<td>manual</td><td class=\"num\">1</td>" in after            # its own ATS bucket
    assert "<td>In pipeline</td><td>Interested</td><td>2</td>" in after   # and in the funnel


def test_add_job_without_company_reports_back_with_values_kept(board_client):
    client, store = board_client
    r = client.post("/board/add", data={**ADD, "company": "  "}, follow_redirects=False)
    assert r.status_code == 400
    assert "Company and title are required." in r.text
    assert "Staff Platform Engineer" in r.text            # typed values survive
    assert '<h1 class="page-title">Applications</h1>' in r.text   # page still renders
    assert not [m for m in store.list_matches() if m["job_id"].startswith("manual:")]


def test_add_job_rejects_a_stage_the_board_cannot_show(board_client):
    client, store = board_client
    r = client.post("/board/add", data={**ADD, "status": "dismissed"}, follow_redirects=False)
    assert r.status_code == 400
    assert not [m for m in store.list_matches() if m["job_id"].startswith("manual:")]


def test_add_job_neutralises_a_hostile_link(board_client):
    client, store = board_client
    client.post("/board/add", data={**ADD, "apply_url": "javascript:alert(1)"})
    assert 'href="#"' in client.get("/board").text
    assert "javascript:alert(1)" not in client.get("/board").text


def test_parse_comp_forms():
    from src.web.board import parse_comp
    assert parse_comp("180000") == 180000
    assert parse_comp("$180,000") == 180000
    assert parse_comp("180k") == 180000
    assert parse_comp("180.5k") == 180500
    assert parse_comp("") is None and parse_comp("  ") is None
    assert parse_comp("lots") is None and parse_comp("0") is None
