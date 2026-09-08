# tests/test_state_sqlite_discovered.py
from src.sqlite_db import connect
from src.state_sqlite import SqliteDiscoveredSlugsStore


def _store():
    return SqliteDiscoveredSlugsStore(connect(":memory:"))


def test_upsert_ok_then_list_healthy():
    s = _store()
    s.upsert_ok("greenhouse:acme", company_name="Acme", last_posting_count=12)
    healthy = s.list_healthy()
    assert len(healthy) == 1
    assert healthy[0].slug == "acme"
    assert healthy[0].validation_status == "ok"
    assert healthy[0].last_posting_count == 12


def test_upsert_failed_increments_then_quarantines():
    s = _store()
    s.upsert_failed("lever:flaky", quarantine_threshold=2)
    assert s.get("lever:flaky").validation_status == "failed"
    assert s.get("lever:flaky").consecutive_failures == 1
    s.upsert_failed("lever:flaky", quarantine_threshold=2)
    assert s.get("lever:flaky").validation_status == "quarantined"


def test_no_match_recency():
    s = _store()
    s.upsert_no_match("ghostco")
    assert s.is_recent_no_match("ghostco", fresh_within_days=30) is True
    assert s.is_recent_no_match("other", fresh_within_days=30) is False


def test_list_for_revalidation_returns_stale_ok_rows():
    s = _store()
    s.upsert_ok("greenhouse:acme")
    # backdate last_validated_at
    import json
    row = s.get("greenhouse:acme")
    s._conn.execute(
        "UPDATE discovered_slugs SET data = ? WHERE connector_name = ?",
        (json.dumps({**row.__dict__, "last_validated_at": "2000-01-01T00:00:00+00:00"}),
         "greenhouse:acme"),
    )
    stale = s.list_for_revalidation(stale_after_days=7, limit=10)
    assert [r.connector_name for r in stale] == ["greenhouse:acme"]


def test_upsert_candidate_and_list_candidates():
    s = _store()
    s.upsert_candidate("greenhouse:newco", company_name="NewCo",
                       origin="hiringcafe", claimed_family="greenhouse")
    rows = s.list_candidates()
    assert len(rows) == 1
    row = rows[0]
    assert row.validation_status == "candidate"
    assert row.origin == "hiringcafe"
    assert row.claimed_family == "greenhouse"
    assert row.sighted_at  # stamped
    assert s.list_healthy() == []  # candidates never poll


def test_upsert_candidate_noops_on_existing_row_any_status():
    s = _store()
    s.upsert_ok("greenhouse:acme", company_name="Acme")
    s.upsert_candidate("greenhouse:acme", company_name="Imposter", origin="hiringcafe")
    assert s.get("greenhouse:acme").validation_status == "ok"
    assert s.get("greenhouse:acme").company_name == "Acme"


def test_upsert_ok_overwrites_candidate_in_place():
    s = _store()
    s.upsert_candidate("lever:fresh", company_name="Fresh", origin="hiringcafe")
    s.upsert_ok("lever:fresh", company_name="Fresh", last_posting_count=3)
    assert s.get("lever:fresh").validation_status == "ok"
    assert s.list_candidates() == []


def test_delete_removes_row():
    s = _store()
    s.upsert_candidate("ashby:gone", origin="hiringcafe")
    s.delete("ashby:gone")
    assert s.get("ashby:gone") is None
    s.delete("ashby:never-existed")  # no-throw


def test_resolve_candidate_no_match_overwrites_in_place():
    s = _store()
    s.upsert_candidate("greenhouse:ghost", company_name="Ghost", origin="hiringcafe")
    s.resolve_candidate_no_match("greenhouse:ghost")
    row = s.get("greenhouse:ghost")
    assert row.validation_status == "no_match"
    assert row.company_name == "Ghost"  # fields preserved
    assert s.list_candidates() == []
    # no-op on a non-candidate row
    s.upsert_ok("lever:live")
    s.resolve_candidate_no_match("lever:live")
    assert s.get("lever:live").validation_status == "ok"


def test_legacy_rows_parse_without_new_fields():
    s = _store()
    s.upsert_ok("greenhouse:old")
    row = s.get("greenhouse:old")
    assert row.origin is None and row.sighted_at is None and row.claimed_family is None
