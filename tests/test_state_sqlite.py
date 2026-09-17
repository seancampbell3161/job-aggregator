# tests/test_state_sqlite.py
import threading
from datetime import datetime, timedelta, timezone

from src.models import NormalizedPosting
from src.sqlite_db import connect
from src.state import VALID_STATUSES
from src.state_sqlite import SqliteSeenJobsStore


def _store():
    return SqliteSeenJobsStore(connect(":memory:"))


def _posting(job_id="greenhouse:acme:1"):
    return NormalizedPosting(
        job_id=job_id, title="Staff Engineer", company="Acme", location_text="Remote",
        location_tags=frozenset(), seniority="staff", stack=frozenset({"python"}),
        comp_min=200000, comp_max=250000, apply_url="https://acme.com/a",
        description="Build things with Python.", posted_at=None, source="greenhouse:acme",
    )


def test_diff_new_returns_unseen():
    s = _store()
    s.mark_seen("a", notified=False)
    assert s.diff_new(["a", "b", "b", "c"]) == ["b", "c"]


def test_claim_for_notify_is_atomic():
    s = _store()
    assert s.claim_for_notify("j1", score=8, posting=_posting("j1")) is True
    assert s.claim_for_notify("j1", score=8, posting=_posting("j1")) is False


def test_claim_then_list_matches_shapes_row():
    s = _store()
    s.claim_for_notify("j1", score=9, rationale="great", gaps=["k8s"], posting=_posting("j1"))
    rows = s.list_matches()
    assert len(rows) == 1
    assert rows[0]["job_id"] == "j1"
    assert rows[0]["title"] == "Staff Engineer"
    assert rows[0]["score"] == 9
    assert rows[0]["gaps"] == ["k8s"]
    assert rows[0]["status"] == "new"


def test_mark_suppressed_excluded_from_matches_but_in_suppressed():
    s = _store()
    s.mark_suppressed("low1", score=3)
    assert s.list_matches() == []
    sup = s.list_suppressed()
    assert sup == [{"score": 3, "first_seen": sup[0]["first_seen"]}]


def test_set_status_appends_history_and_clears_ttl():
    s = _store()
    s.claim_for_notify("j1", score=9, posting=_posting("j1"))
    assert s.set_status("j1", "applied") is True
    m = s.get_match("j1")
    assert m["status"] == "applied"
    assert [h["status"] for h in m["history"]] == ["applied"]
    # kept status removes ttl -> row survives prune
    s.prune_expired()
    assert s.get_match("j1") is not None


def test_set_status_missing_row_returns_false():
    assert _store().set_status("nope", "applied") is False


def test_recent_gap_lists_filters_by_since():
    s = _store()
    s.claim_for_notify("j1", score=9, gaps=["k8s"], posting=_posting("j1"))
    since = datetime.now(timezone.utc) - timedelta(days=1)
    assert s.recent_gap_lists(since) == [["k8s"]]
    future = datetime.now(timezone.utc) + timedelta(days=1)
    assert s.recent_gap_lists(future) == []


def test_expired_rows_hidden_and_pruned():
    s = _store()
    s.claim_for_notify("j1", score=9, posting=_posting("j1"))
    # force-expire the row
    s._conn.execute("UPDATE seen_jobs SET ttl = 1 WHERE job_id = 'j1'")
    assert s.get_match("j1") is None
    assert s.diff_new(["j1"]) == ["j1"]
    s.prune_expired()
    assert s._conn.execute("SELECT COUNT(*) FROM seen_jobs").fetchone()[0] == 0


def test_set_status_on_expired_row_returns_false():
    s = _store()
    s.claim_for_notify("j1", score=9, posting=_posting("j1"))
    # force-expire the row
    s._conn.execute("UPDATE seen_jobs SET ttl = 1 WHERE job_id = 'j1'")
    assert s.set_status("j1", "applied") is False


def test_get_jd_returns_snapshot():
    s = _store()
    s.claim_for_notify("j1", posting=_posting("j1"))
    jd = s.get_jd("j1")
    assert jd.description == "Build things with Python."
    assert jd.title == "Staff Engineer"
    assert s.get_jd("missing") is None


def test_get_jd_sanitizes_legacy_unsanitized_snapshot():
    """Rows written before the injection filter existed hold raw text; the
    tailor prompt reads through get_jd, so sanitization must happen on read."""
    import json
    from src.sanitize import _MARKER
    from src.sqlite_db import connect
    from src.state_sqlite import SqliteSeenJobsStore
    from tests.test_sanitize import TRIGGERDEV

    s = SqliteSeenJobsStore(connect(":memory:"))
    item = {"job_id": "legacy:1", "notified": True, "first_seen": "2026-07-01T00:00:00+00:00",
            "title": "SWE", "company": "Acme", "description_snapshot": TRIGGERDEV}
    s._conn.execute(
        "INSERT INTO seen_jobs (job_id, first_seen, notified, ttl, score, title, data) "
        "VALUES (?, ?, 1, NULL, NULL, ?, ?)",
        (item["job_id"], item["first_seen"], item["title"], json.dumps(item)),
    )
    jd = s.get_jd("legacy:1")
    assert jd is not None
    assert "ignore all previous instructions" not in jd.description.lower()
    assert _MARKER in jd.description


def test_set_status_concurrent_no_transaction_error():
    """Regression: concurrent set_status on a shared connection must not raise
    'cannot start a transaction within a transaction'. Before the fix, two threads
    racing on BEGIN IMMEDIATE would produce OperationalError; the threading.Lock
    in __init__ serializes the multi-statement transaction block."""
    # Build ONE store over a shared connection (mimics the web process singleton).
    s = SqliteSeenJobsStore(connect(":memory:"))
    s.claim_for_notify("j1", score=9, posting=_posting("j1"))

    statuses = [s for s in VALID_STATUSES if s != "new"]  # exclude default; pick real ones
    errors: list[Exception] = []

    def worker(idx: int) -> None:
        status = statuses[idx % len(statuses)]
        for _ in range(40):
            try:
                s.set_status("j1", status)
            except Exception as exc:
                errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"Concurrent set_status raised {len(errors)} error(s): {errors[:3]}"
    match = s.get_match("j1")
    assert match is not None
    assert match["status"] in VALID_STATUSES


def _norm(job_id="greenhouse:acme:9", title="Platform Engineer"):
    from datetime import datetime, timezone
    from src.models import NormalizedPosting
    return NormalizedPosting(
        job_id=job_id, title=title, company="Acme", location_text="Remote (US)",
        location_tags=frozenset({"remote", "us"}), seniority="senior",
        stack=frozenset({"python"}), comp_min=150000, comp_max=200000,
        apply_url="https://a/9", description="Build platforms with Python.",
        posted_at=datetime(2026, 7, 1, tzinfo=timezone.utc), source="greenhouse:acme",
    )


def test_mark_suppressed_stores_rationale_and_display_fields():
    from src.sqlite_db import connect
    from src.state_sqlite import SqliteSeenJobsStore
    from tests.sqlite_helpers import raw_seen_item
    s = SqliteSeenJobsStore(connect(":memory:"))
    s.mark_suppressed("greenhouse:acme:9", score=3, rationale="Weak fit", posting=_norm())
    rows = s.list_suppressed_details(since_iso="")
    assert len(rows) == 1
    it = rows[0]
    assert it["score"] == 3
    assert it["rationale"] == "Weak fit"
    assert it["title"] == "Platform Engineer"
    assert it["description_snapshot"] == "Build platforms with Python."
    raw = raw_seen_item(s, "greenhouse:acme:9")
    assert raw["notified"] is False


def test_mark_suppressed_without_details_still_works():
    from src.sqlite_db import connect
    from src.state_sqlite import SqliteSeenJobsStore
    s = SqliteSeenJobsStore(connect(":memory:"))
    s.mark_suppressed("x:1", score=2)  # old call shape
    assert s.list_suppressed() == [
        {"score": 2, "first_seen": s.list_suppressed_details(since_iso="")[0]["first_seen"]}
    ]


def test_get_suppressed_only_returns_suppressed_rows():
    from src.sqlite_db import connect
    from src.state_sqlite import SqliteSeenJobsStore
    s = SqliteSeenJobsStore(connect(":memory:"))
    s.mark_suppressed("x:1", score=2, posting=_norm(job_id="x:1"))
    s.claim_for_notify("x:2", score=8, posting=_norm(job_id="x:2"))
    assert s.get_suppressed("x:1") is not None
    assert s.get_suppressed("x:2") is None   # notified row, not suppressed
    assert s.get_suppressed("x:3") is None   # absent


def test_set_audit_verdict_roundtrip_and_validation():
    import pytest
    from src.sqlite_db import connect
    from src.state_sqlite import SqliteSeenJobsStore
    s = SqliteSeenJobsStore(connect(":memory:"))
    s.mark_suppressed("x:1", score=2, posting=_norm(job_id="x:1"))
    assert s.set_audit_verdict("x:1", "confirmed_rejected") is True
    it = s.list_suppressed_details(since_iso="")[0]
    assert it["audit_verdict"] == "confirmed_rejected"
    assert it["audit_verdict_at"]
    assert s.set_audit_verdict("nope:1", "rescued") is False
    with pytest.raises(ValueError):
        s.set_audit_verdict("x:1", "bogus")


def test_set_audit_verdict_clears_ttl_column_for_retention():
    """A confirmed_rejected suppressed row must survive its 60-day TTL so the
    tuning CLI can eventually accumulate ~10 verdicts; _live_rows and
    prune_expired both key off the ttl COLUMN, so clearing only the JSON
    field would not be enough — the column itself must go to NULL."""
    from src.sqlite_db import connect
    from src.state_sqlite import SqliteSeenJobsStore
    s = SqliteSeenJobsStore(connect(":memory:"))
    s.mark_suppressed("x:1", score=2, posting=_norm(job_id="x:1"))
    assert s.set_audit_verdict("x:1", "confirmed_rejected") is True

    ttl = s._conn.execute("SELECT ttl FROM seen_jobs WHERE job_id = 'x:1'").fetchone()[0]
    assert ttl is None
    # survives prune_expired (the real proof it outlives the 60-day TTL window)
    s.prune_expired()
    assert s.list_suppressed_details(since_iso="2000-01-01") != []


def test_list_suppressed_details_respects_since():
    from src.sqlite_db import connect
    from src.state_sqlite import SqliteSeenJobsStore
    s = SqliteSeenJobsStore(connect(":memory:"))
    s.mark_suppressed("x:1", score=2, posting=_norm(job_id="x:1"))
    assert s.list_suppressed_details(since_iso="2020-01-01") != []
    assert s.list_suppressed_details(since_iso="2999-01-01") == []


def test_mark_rescued_from_suppression_roundtrip_and_ttl_cleared():
    """The /audit rescue route for a score_low-origin job: release_claim deletes
    the suppressed row, claim_for_notify inserts a fresh notified row, then
    mark_rescued_from_suppression stamps the rescue verdict + ORIGINAL
    suppressed score onto that row and clears the ttl COLUMN (not just the
    JSON) so it survives past the 60-day TTL — _live_rows filters on the ttl
    column, so leaving it set would let the rescued label silently expire."""
    from src.sqlite_db import connect
    from src.state_sqlite import SqliteSeenJobsStore
    s = SqliteSeenJobsStore(connect(":memory:"))
    s.mark_suppressed("x:1", score=2, posting=_norm(job_id="x:1"))
    # simulate the /audit rescue transition
    s.release_claim("x:1")
    s.claim_for_notify("x:1", score=7, posting=_norm(job_id="x:1"))

    assert s.mark_rescued_from_suppression("x:1", suppressed_score=2) is True

    rows = s.list_rescued_suppressions(since_iso="")
    assert len(rows) == 1
    assert rows[0]["score"] == 2  # the ORIGINAL suppressed score, not the re-score (7)
    assert rows[0]["audit_verdict"] == "rescued"

    ttl = s._conn.execute("SELECT ttl FROM seen_jobs WHERE job_id = 'x:1'").fetchone()[0]
    assert ttl is None
    # survives prune_expired (the real proof it outlives the 60-day TTL window)
    s.prune_expired()
    assert s.list_rescued_suppressions(since_iso="2000-01-01") != []


def test_mark_rescued_from_suppression_absent_job_returns_false():
    s = _store()
    assert s.mark_rescued_from_suppression("nope:1", suppressed_score=2) is False


def test_update_closed_check_two_strike_roundtrip():
    from src.sqlite_db import connect
    from src.state_sqlite import SqliteSeenJobsStore
    s = SqliteSeenJobsStore(connect(":memory:"))
    s.claim_for_notify("x:1", score=7, posting=_norm(job_id="x:1"))
    s.set_status("x:1", "applied")

    assert s.update_closed_check("x:1", misses=1, closed_at=None) is True
    m = s.get_match("x:1")
    assert m["closed_misses"] == 1 and m["posting_closed_at"] is None

    assert s.update_closed_check("x:1", misses=2, closed_at="2026-07-02T04:30:00+00:00") is True
    assert s.mark_closed_notified("x:1") is True
    m = s.get_match("x:1")
    assert m["posting_closed_at"] == "2026-07-02T04:30:00+00:00"
    assert m["closed_notified"] is True

    # alive again: reset clears the flag AND the notified marker
    assert s.update_closed_check("x:1", misses=0, closed_at=None) is True
    m = s.get_match("x:1")
    assert m["closed_misses"] == 0
    assert m["posting_closed_at"] is None
    assert m["closed_notified"] is False

    assert s.update_closed_check("gone:1", misses=1, closed_at=None) is False
    assert s.mark_closed_notified("gone:1") is False


def _suggestion(msg_id="<m1@ats>", status="rejected"):
    return {
        "suggested_status": status, "subject": "Update on your application",
        "from": "no-reply@us.greenhouse-mail.io",
        "date": "2026-07-02T14:03:11+00:00",
        "matched_phrase": "unfortunately", "message_id": msg_id,
    }


def _claimed_store():
    s = _store()
    s.claim_for_notify("j1", score=7, posting=_posting("j1"))
    return s


def test_email_suggestion_round_trip():
    s = _claimed_store()
    assert s.update_email_suggestion("j1", suggestion=_suggestion())
    m = s.get_match("j1")
    assert m["email_suggestion"]["suggested_status"] == "rejected"
    assert m["email_suggestion"]["message_id"] == "<m1@ats>"
    assert m["dismissed_suggestions"] == []


def test_email_suggestion_clear():
    s = _claimed_store()
    s.update_email_suggestion("j1", suggestion=_suggestion())
    assert s.update_email_suggestion("j1", suggestion=None)
    m = s.get_match("j1")
    assert m["email_suggestion"] is None
    assert m["dismissed_suggestions"] == []


def test_email_suggestion_dismiss_records_message_id():
    s = _claimed_store()
    s.update_email_suggestion("j1", suggestion=_suggestion())
    s.update_email_suggestion("j1", suggestion=None, record_dismissed=True)
    m = s.get_match("j1")
    assert m["email_suggestion"] is None
    assert m["dismissed_suggestions"] == ["<m1@ats>"]


def test_email_suggestion_dismissed_list_capped_at_20():
    s = _claimed_store()
    for i in range(25):
        s.update_email_suggestion("j1", suggestion=_suggestion(msg_id=f"<m{i}@ats>"))
        s.update_email_suggestion("j1", suggestion=None, record_dismissed=True)
    m = s.get_match("j1")
    assert len(m["dismissed_suggestions"]) == 20
    assert m["dismissed_suggestions"][-1] == "<m24@ats>"


def test_email_suggestion_vanished_row_returns_false():
    s = _claimed_store()
    assert s.update_email_suggestion("nope:1", suggestion=_suggestion()) is False


def test_list_score_rows_returns_scored_rows_with_company(tmp_path):
    from datetime import datetime, timezone
    from src.models import NormalizedPosting
    from src.sqlite_db import connect
    from src.state_sqlite import SqliteSeenJobsStore

    store = SqliteSeenJobsStore(connect(str(tmp_path / "t.db")))

    def posting(job_id, source):
        return NormalizedPosting(
            job_id=job_id, title="Staff Software Engineer", company="Acme Robotics",
            location_text="Austin, TX", location_tags=frozenset(), seniority="staff",
            stack=frozenset(), comp_min=None, comp_max=None,
            apply_url=f"https://a/{job_id}", description="d",
            posted_at=datetime(2026, 7, 17, tzinfo=timezone.utc), source=source,
        )

    store.claim_for_notify("adzuna:5001", score=5, posting=posting("adzuna:5001", "adzuna"))
    store.mark_suppressed("greenhouse:acme:9", score=3,
                          posting=posting("greenhouse:acme:9", "greenhouse:acme"))
    store.mark_seen("lever:x:1", notified=False)  # unscored — excluded

    rows = store.list_score_rows()
    assert {r["job_id"] for r in rows} == {"adzuna:5001", "greenhouse:acme:9"}
    by_id = {r["job_id"]: r for r in rows}
    assert by_id["adzuna:5001"]["score"] == 5
    assert by_id["adzuna:5001"]["notified"] is True
    assert by_id["adzuna:5001"]["company"] == "Acme Robotics"
    assert by_id["greenhouse:acme:9"]["notified"] is False
    assert by_id["adzuna:5001"]["first_seen"]  # ISO timestamp present
