# tests/test_state_sqlite_rejected.py
import json
import logging
import sqlite3
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


# --- compressed job text (description_z) ---

_PROSE = (
    "We are looking for an engineer to join our platform team. You will design, "
    "build and operate backend services that power our customer-facing products. "
    "Responsibilities include code review, mentoring teammates, and on-call. "
    "Requirements: 3+ years of experience, strong communication, and a bias for "
    "shipping. Benefits: health insurance, equity, flexible hours, remote-friendly. "
) * 40


def _long_posting(job_id="a:long", description=_PROSE):
    p = _posting(job_id)
    return NormalizedPosting(**{**p.__dict__, "description": description})


def _raw(s, job_id):
    return s._conn.execute(
        "SELECT data, description_z FROM rejected_postings WHERE job_id = ?",
        (job_id,)).fetchone()


def test_description_roundtrips_via_get_list_and_record_many():
    s = _store()
    s.record(_long_posting("a:1"), rejected_by="role")
    s.record_many([(_long_posting("a:2"), "role")])
    expected = s.get("a:1")["description_snapshot"]
    assert expected and len(expected) > 1000
    assert s.get("a:2")["description_snapshot"] == expected
    listed = {r["job_id"]: r for r in s.list_rejected(since_iso="")}
    assert listed["a:1"]["description_snapshot"] == expected
    assert listed["a:2"]["description_snapshot"] == expected


def test_description_stored_compressed_not_in_data_json():
    s = _store()
    s.record(_long_posting("a:1"), rejected_by="role")
    row = _raw(s, "a:1")
    assert "description_snapshot" not in json.loads(row["data"])
    blob = row["description_z"]
    assert isinstance(blob, bytes)
    assert len(blob) < 0.5 * len(s.get("a:1")["description_snapshot"].encode("utf-8"))


def test_legacy_row_with_text_in_data_reads_unchanged():
    s = _store()
    item = {"job_id": "old:1", "first_seen": "2026-01-01T00:00:00+00:00",
            "rejected_by": "role", "title": "T", "description_snapshot": "legacy text"}
    s._conn.execute(
        "INSERT INTO rejected_postings (job_id, first_seen, rejected_by, data) "
        "VALUES (?,?,?,?)",
        ("old:1", item["first_seen"], "role", json.dumps(item)))
    assert s.get("old:1")["description_snapshot"] == "legacy text"
    assert s.list_rejected(since_iso="")[0]["description_snapshot"] == "legacy text"


def test_no_description_stores_null_and_omits_snapshot():
    s = _store()
    s.record(_long_posting("a:1", description=""), rejected_by="role")
    assert _raw(s, "a:1")["description_z"] is None
    assert "description_snapshot" not in s.get("a:1")
    assert "description_snapshot" not in s.list_rejected(since_iso="")[0]


def test_corrupt_blob_does_not_break_reads(caplog):
    s = _store()
    s.record(_long_posting("a:1"), rejected_by="role")
    s._conn.execute("UPDATE rejected_postings SET description_z = ? WHERE job_id = ?",
                    (b"not zlib data", "a:1"))
    with caplog.at_level(logging.WARNING):
        assert "description_snapshot" not in s.get("a:1")
        assert "description_snapshot" not in s.list_rejected(since_iso="")[0]
    corrupt = [r for r in caplog.records if r.message == "audit_description_corrupt"]
    assert corrupt and all(r.job_id == "a:1" for r in corrupt)


def test_connect_adds_description_z_to_old_schema(tmp_path):
    path = str(tmp_path / "old.db")
    old = sqlite3.connect(path)
    old.execute(
        "CREATE TABLE rejected_postings (job_id TEXT PRIMARY KEY, first_seen TEXT NOT NULL,"
        " rejected_by TEXT NOT NULL, title TEXT, company TEXT, location_text TEXT,"
        " source TEXT, apply_url TEXT, posted_at TEXT, comp_min INTEGER, comp_max INTEGER,"
        " verdict TEXT, verdict_at TEXT, data TEXT NOT NULL)")
    old.commit()
    old.close()
    conn = connect(path)
    assert "description_z" in {r[1] for r in conn.execute("PRAGMA table_info(rejected_postings)")}
    s = SqliteRejectedPostingsStore(conn)
    s.record(_long_posting("a:1"), rejected_by="role")
    assert s.get("a:1")["description_snapshot"].startswith("We are looking")

