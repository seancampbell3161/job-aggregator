# tests/test_state_sqlite_rejected.py
from datetime import datetime, timezone

import pytest

from src.models import NormalizedPosting
from src.sqlite_db import connect
from src.state_sqlite import SqliteRejectedPostingsStore


def _store():
    return SqliteRejectedPostingsStore(connect(":memory:"))


def _posting(job_id="greenhouse:acme:1", title="Office Manager", company="Acme"):
    return NormalizedPosting(
        job_id=job_id, title=title, company=company, location_text="Austin, TX",
        location_tags=frozenset({"austin"}), seniority="mid",
        stack=frozenset(), comp_min=None, comp_max=None,
        apply_url="https://a/1", description="Run the office.",
        posted_at=datetime(2026, 7, 1, tzinfo=timezone.utc), source="greenhouse:acme",
    )


def test_record_once_and_roundtrip():
    s = _store()
    assert s.record(_posting(), rejected_by="role") is True
    assert s.record(_posting(), rejected_by="location") is False  # capture-once
    rows = s.list_rejected(since_iso="")
    assert len(rows) == 1
    it = rows[0]
    assert it["rejected_by"] == "role"          # first rejection wins
    assert it["title"] == "Office Manager"
    assert it["description_snapshot"] == "Run the office."
    assert it["verdict"] is None


def test_list_rejected_filters():
    s = _store()
    s.record(_posting("a:1", title="Office Manager"), rejected_by="role")
    s.record(_posting("a:2", title="Software Engineer", company="Berlin GmbH"),
             rejected_by="location")
    assert [r["job_id"] for r in s.list_rejected(since_iso="", gate="role")] == ["a:1"]
    assert [r["job_id"] for r in s.list_rejected(since_iso="", query="berlin")] == ["a:2"]
    assert s.list_rejected(since_iso="2999-01-01") == []


def test_counts_by_gate():
    s = _store()
    s.record(_posting("a:1"), rejected_by="role")
    s.record(_posting("a:2"), rejected_by="role")
    s.record(_posting("a:3"), rejected_by="comp")
    assert s.counts_by_gate(since_iso="") == {"role": 2, "comp": 1}


def test_set_verdict_and_judged_filter():
    s = _store()
    s.record(_posting("a:1"), rejected_by="role")
    assert s.set_verdict("a:1", "confirmed_rejected") is True
    assert s.set_verdict("missing:1", "rescued") is False
    with pytest.raises(ValueError):
        s.set_verdict("a:1", "bogus")
    assert s.list_rejected(since_iso="", include_judged=False) == []
    judged = s.list_rejected(since_iso="")[0]
    assert judged["verdict"] == "confirmed_rejected"
    assert judged["verdict_at"]


def test_get_returns_item_or_none():
    s = _store()
    s.record(_posting("a:1"), rejected_by="role")
    assert s.get("a:1")["job_id"] == "a:1"
    assert s.get("missing") is None


def test_prune_older_than():
    s = _store()
    s.record(_posting("a:1"), rejected_by="role")
    s._conn.execute(
        "UPDATE rejected_postings SET first_seen = '2020-01-01T00:00:00+00:00' WHERE job_id = 'a:1'"
    )
    s.record(_posting("a:2"), rejected_by="role")
    assert s.prune_older_than(90) == 1
    assert [r["job_id"] for r in s.list_rejected(since_iso="")] == ["a:2"]


def test_prune_older_than_spares_verdicted_rows():
    """A verdicted row (rescued/confirmed_rejected) must survive prune even
    when it's past the retention cutoff — otherwise the tuning CLI's sample
    of ~10 verdicts never gets a chance to accumulate before rows age out."""
    s = _store()
    s.record(_posting("a:1"), rejected_by="role")
    s.record(_posting("a:2"), rejected_by="role")
    assert s.set_verdict("a:1", "confirmed_rejected") is True
    s._conn.execute(
        "UPDATE rejected_postings SET first_seen = '2020-01-01T00:00:00+00:00'"
    )
    assert s.prune_older_than(30) == 1
    ids = [r["job_id"] for r in s.list_rejected(since_iso="")]
    assert ids == ["a:1"]


def test_record_many_is_capture_once_with_the_same_rows_as_record():
    s = _store()
    s.record(_posting("a:1"), rejected_by="role")
    inserted = s.record_many([
        (_posting("a:1"), "location"),      # already recorded: first rejection wins
        (_posting("a:2", title="Barista"), "role"),
        (_posting("a:3", title="Chef"), "comp"),
    ])
    assert inserted == 2
    rows = {r["job_id"]: r for r in s.list_rejected(since_iso="")}
    assert set(rows) == {"a:1", "a:2", "a:3"}
    assert rows["a:1"]["rejected_by"] == "role"
    assert rows["a:3"]["rejected_by"] == "comp"
    single = _store()
    single.record(_posting("a:2", title="Barista"), rejected_by="role")
    via_record = single.list_rejected(since_iso="")[0]
    assert {k: v for k, v in rows["a:2"].items() if k != "first_seen"} == \
        {k: v for k, v in via_record.items() if k != "first_seen"}


def test_record_many_empty_is_noop():
    assert _store().record_many([]) == 0


def test_record_many_writes_all_or_nothing():
    s = _store()
    s._conn.execute(
        "CREATE TRIGGER boom BEFORE INSERT ON rejected_postings "
        "WHEN NEW.job_id = 'a:3' BEGIN SELECT RAISE(ABORT, 'boom'); END")
    with pytest.raises(Exception, match="boom"):
        s.record_many([(_posting("a:2"), "role"), (_posting("a:3"), "role")])
    assert s.list_rejected(since_iso="") == []
    assert not s._conn.in_transaction
