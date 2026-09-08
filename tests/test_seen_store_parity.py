from datetime import datetime, timedelta, timezone

from src.models import NormalizedPosting


def _p(job_id):
    return NormalizedPosting(
        job_id=job_id, title="Staff Eng", company="Acme", location_text="Remote",
        location_tags=frozenset(), seniority="staff", stack=frozenset({"python"}),
        comp_min=200000, comp_max=250000, apply_url="https://x", description="JD body",
        posted_at=None, source="greenhouse:acme",
    )


def test_claim_is_atomic(seen_store):
    assert seen_store.claim_for_notify("j1", score=8, posting=_p("j1")) is True
    assert seen_store.claim_for_notify("j1", score=8, posting=_p("j1")) is False


def test_diff_new(seen_store):
    seen_store.mark_seen("a", notified=False)
    assert seen_store.diff_new(["a", "b"]) == ["b"]


def test_list_matches_shape(seen_store):
    seen_store.claim_for_notify("j1", score=9, gaps=["k8s"], posting=_p("j1"))
    rows = seen_store.list_matches()
    row = rows[0]
    assert len(rows) == 1
    assert row["job_id"] == "j1"
    assert row["gaps"] == ["k8s"]
    assert row["status"] == "new"
    assert row["score"] == 9


def test_set_status_history(seen_store):
    seen_store.claim_for_notify("j1", score=9, posting=_p("j1"))
    assert seen_store.set_status("j1", "applied") is True
    assert seen_store.get_match("j1")["status"] == "applied"
    assert seen_store.get_match("j1")["history"][-1]["status"] == "applied"


def test_recent_gap_lists(seen_store):
    seen_store.claim_for_notify("j1", score=9, gaps=["k8s"], posting=_p("j1"))
    since = datetime.now(timezone.utc) - timedelta(days=1)
    assert seen_store.recent_gap_lists(since) == [["k8s"]]


def test_get_jd(seen_store):
    seen_store.claim_for_notify("j1", posting=_p("j1"))
    assert seen_store.get_jd("j1").description == "JD body"


def test_mark_suppressed_detail_parity_both_backends():
    """Both backends persist rationale + title on suppressed rows."""
    from datetime import datetime, timezone
    import boto3
    from moto import mock_aws
    from src.models import NormalizedPosting
    from src.sqlite_db import connect
    from src.state import SeenJobsStore
    from src.state_sqlite import SqliteSeenJobsStore

    posting = NormalizedPosting(
        job_id="parity:sup:1", title="Platform Engineer", company="Acme",
        location_text="Remote (US)", location_tags=frozenset({"remote", "us"}),
        seniority="senior", stack=frozenset({"python"}), comp_min=None,
        comp_max=None, apply_url="https://a/1", description="d",
        posted_at=datetime(2026, 7, 1, tzinfo=timezone.utc), source="greenhouse:acme",
    )

    sq = SqliteSeenJobsStore(connect(":memory:"))
    sq.mark_suppressed("parity:sup:1", score=3, rationale="Weak fit", posting=posting)
    it = sq.list_suppressed_details(since_iso="")[0]
    assert (it["rationale"], it["title"]) == ("Weak fit", "Platform Engineer")

    with mock_aws():
        ddb = boto3.client("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName="seen_jobs_parity",
            AttributeDefinitions=[{"AttributeName": "job_id", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "job_id", "KeyType": "HASH"}],
            BillingMode="PAY_PER_REQUEST",
        )
        dd = SeenJobsStore(table_name="seen_jobs_parity")
        dd.mark_suppressed("parity:sup:1", score=3, rationale="Weak fit", posting=posting)
        item = dd._table.get_item(Key={"job_id": "parity:sup:1"})["Item"]
        assert item["rationale"] == "Weak fit"
        assert item["title"] == "Platform Engineer"
        assert item["notified"] is False
