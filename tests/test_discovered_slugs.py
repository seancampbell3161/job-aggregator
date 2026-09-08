import boto3
import pytest
from moto import mock_aws

from src.state import DiscoveredSlugsStore

TABLE = "discovered_slugs_test"


@pytest.fixture
def store():
    with mock_aws():
        ddb = boto3.client("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName=TABLE,
            AttributeDefinitions=[{"AttributeName": "connector_name", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "connector_name", "KeyType": "HASH"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield DiscoveredSlugsStore(table_name=TABLE)


def test_upsert_ok_then_list_healthy(store):
    store.upsert_ok("greenhouse:stripe", company_name="Stripe", last_posting_count=42)
    healthy = store.list_healthy()
    assert len(healthy) == 1
    row = healthy[0]
    assert row.connector_name == "greenhouse:stripe"
    assert row.ats_family == "greenhouse"
    assert row.slug == "stripe"
    assert row.company_name == "Stripe"
    assert row.validation_status == "ok"
    assert row.consecutive_failures == 0


def test_upsert_failed_increments_failures(store):
    store.upsert_failed("greenhouse:doesnotexist")
    rows = store.list_all()
    assert len(rows) == 1
    assert rows[0].validation_status == "failed"
    assert rows[0].consecutive_failures == 1

    store.upsert_failed("greenhouse:doesnotexist")
    rows = store.list_all()
    assert rows[0].consecutive_failures == 2


def test_quarantine_after_threshold(store):
    for _ in range(5):
        store.upsert_failed("greenhouse:doomed", quarantine_threshold=5)
    rows = store.list_all()
    assert rows[0].validation_status == "quarantined"
    assert rows[0].consecutive_failures == 5


def test_list_healthy_excludes_failed_and_quarantined(store):
    store.upsert_ok("greenhouse:good", company_name="Good")
    store.upsert_failed("greenhouse:bad")
    for _ in range(5):
        store.upsert_failed("greenhouse:doomed", quarantine_threshold=5)
    healthy = store.list_healthy()
    assert {r.connector_name for r in healthy} == {"greenhouse:good"}


def test_upsert_ok_resets_failures(store):
    store.upsert_failed("greenhouse:flaky")
    store.upsert_failed("greenhouse:flaky")
    store.upsert_ok("greenhouse:flaky", company_name="Flaky")
    rows = store.list_all()
    assert rows[0].consecutive_failures == 0
    assert rows[0].validation_status == "ok"


def test_get_returns_none_for_missing(store):
    assert store.get("greenhouse:never-seen") is None


from datetime import datetime, timedelta, timezone


def test_upsert_no_match_writes_row(store):
    store.upsert_no_match("randomstartup")
    rows = store.list_all()
    assert len(rows) == 1
    row = rows[0]
    assert row.connector_name == "nomatch:randomstartup"
    assert row.validation_status == "no_match"


def test_list_healthy_excludes_no_match(store):
    """list_healthy() must continue to return only ok rows."""
    store.upsert_ok("greenhouse:good", company_name="Good")
    store.upsert_no_match("badslug")
    healthy = store.list_healthy()
    assert {r.connector_name for r in healthy} == {"greenhouse:good"}


def test_is_recent_no_match_returns_true_when_recent(store):
    store.upsert_no_match("randomstartup")
    assert store.is_recent_no_match("randomstartup", fresh_within_days=90) is True


def test_is_recent_no_match_returns_false_when_unknown(store):
    assert store.is_recent_no_match("never-seen", fresh_within_days=90) is False


def test_is_recent_no_match_returns_false_when_stale():
    """A no_match row older than fresh_within_days is treated as expired."""
    import boto3
    from moto import mock_aws
    from src.state import DiscoveredSlugsStore

    with mock_aws():
        ddb = boto3.client("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName="discovered_slugs_t2",
            AttributeDefinitions=[{"AttributeName": "connector_name", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "connector_name", "KeyType": "HASH"}],
            BillingMode="PAY_PER_REQUEST",
        )
        s = DiscoveredSlugsStore(table_name="discovered_slugs_t2")
        # Manually insert a row with last_validated_at 100 days ago.
        old = (datetime.now(timezone.utc) - timedelta(days=100)).isoformat()
        s._table.put_item(Item={
            "connector_name": "nomatch:oldslug",
            "ats_family": "nomatch",
            "slug": "oldslug",
            "validation_status": "no_match",
            "discovered_at": old,
            "last_validated_at": old,
            "consecutive_failures": 0,
            "last_posting_count": 0,
        })
        assert s.is_recent_no_match("oldslug", fresh_within_days=90) is False


def test_list_for_revalidation_returns_stale_ok_rows():
    """Only ok rows older than threshold get returned, and we respect limit."""
    import boto3
    from moto import mock_aws
    from src.state import DiscoveredSlugsStore

    with mock_aws():
        ddb = boto3.client("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName="discovered_slugs_t3",
            AttributeDefinitions=[{"AttributeName": "connector_name", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "connector_name", "KeyType": "HASH"}],
            BillingMode="PAY_PER_REQUEST",
        )
        s = DiscoveredSlugsStore(table_name="discovered_slugs_t3")

        recent = datetime.now(timezone.utc).isoformat()
        old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()

        for cn, status, last_val in [
            ("greenhouse:fresh-ok", "ok", recent),
            ("greenhouse:stale-ok-1", "ok", old),
            ("greenhouse:stale-ok-2", "ok", old),
            ("greenhouse:stale-failed", "failed", old),
            ("nomatch:stale-nomatch", "no_match", old),
        ]:
            ats, slug = cn.split(":", 1)
            s._table.put_item(Item={
                "connector_name": cn,
                "ats_family": ats,
                "slug": slug,
                "validation_status": status,
                "discovered_at": recent,
                "last_validated_at": last_val,
                "consecutive_failures": 0,
                "last_posting_count": 1,
            })

        rows = s.list_for_revalidation(stale_after_days=7, limit=10)
        names = {r.connector_name for r in rows}
        assert names == {"greenhouse:stale-ok-1", "greenhouse:stale-ok-2"}


def test_list_for_revalidation_respects_limit():
    import boto3
    from moto import mock_aws
    from src.state import DiscoveredSlugsStore

    with mock_aws():
        ddb = boto3.client("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName="discovered_slugs_t4",
            AttributeDefinitions=[{"AttributeName": "connector_name", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "connector_name", "KeyType": "HASH"}],
            BillingMode="PAY_PER_REQUEST",
        )
        s = DiscoveredSlugsStore(table_name="discovered_slugs_t4")

        old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        for i in range(10):
            s._table.put_item(Item={
                "connector_name": f"greenhouse:stale-{i}",
                "ats_family": "greenhouse",
                "slug": f"stale-{i}",
                "validation_status": "ok",
                "discovered_at": old,
                "last_validated_at": old,
                "consecutive_failures": 0,
                "last_posting_count": 1,
            })

        rows = s.list_for_revalidation(stale_after_days=7, limit=3)
        assert len(rows) == 3


def test_candidate_lifecycle_dynamo(store):
    store.upsert_candidate("greenhouse:newco", company_name="NewCo",
                           origin="hiringcafe", claimed_family="greenhouse")
    assert len(store.list_candidates()) == 1
    assert store.list_healthy() == []
    store.upsert_candidate("greenhouse:newco", company_name="Imposter")  # no-op
    assert store.get("greenhouse:newco").company_name == "NewCo"
    store.resolve_candidate_no_match("greenhouse:newco")
    assert store.get("greenhouse:newco").validation_status == "no_match"
    store.delete("greenhouse:newco")
    assert store.get("greenhouse:newco") is None


# ---------- conversion-chain no_match learning (spec part 2) ----------

from src.sqlite_db import connect
from src.state import no_match_exhausted
from src.state_sqlite import SqliteDiscoveredSlugsStore


def _sqlite_store():
    return SqliteDiscoveredSlugsStore(connect(":memory:"))


def test_no_match_learning_fields_round_trip_dynamo(store):
    store.upsert_no_match(
        "diode-computers",
        company_name="Diode Computers, Inc.",
        website="https://diode.dev",
        methods_tried=["slug:diode-computers", "domain:diode", "fingerprint"],
    )
    row = store.get("nomatch:diode-computers")
    assert row.validation_status == "no_match"
    assert row.company_name == "Diode Computers, Inc."
    assert row.website == "https://diode.dev"
    assert row.methods_tried == ["slug:diode-computers", "domain:diode", "fingerprint"]


def test_no_match_learning_fields_round_trip_sqlite():
    s = _sqlite_store()
    s.upsert_no_match(
        "diode-computers",
        company_name="Diode Computers, Inc.",
        website="https://diode.dev",
        methods_tried=["slug:diode-computers", "fingerprint"],
    )
    row = s.get("nomatch:diode-computers")
    assert row.website == "https://diode.dev"
    assert row.methods_tried == ["slug:diode-computers", "fingerprint"]


def test_plain_upsert_no_match_preserves_learned_fields(store):
    store.upsert_no_match("acme", company_name="Acme", website="https://acme.com",
                          methods_tried=["slug:acme"])
    store.upsert_no_match("acme")  # legacy plain call (e.g. _validate_slug_candidate)
    row = store.get("nomatch:acme")
    assert row.company_name == "Acme"
    assert row.website == "https://acme.com"
    assert row.methods_tried == ["slug:acme"]


def test_legacy_no_match_rows_parse_with_none_fields(store):
    store.upsert_no_match("oldco")
    row = store.get("nomatch:oldco")
    assert row.website is None
    assert row.methods_tried is None


def test_list_no_match_both_stores(store):
    store.upsert_no_match("a")
    store.upsert_ok("greenhouse:b", company_name="B")
    assert {r.slug for r in store.list_no_match()} == {"a"}
    s = _sqlite_store()
    s.upsert_no_match("c")
    s.upsert_ok("greenhouse:d", company_name="D")
    assert {r.slug for r in s.list_no_match()} == {"c"}


# ---------- exhaustion classifier ----------


def _nm(website=None, methods_tried=None):
    from src.state import DiscoveredSlug
    return DiscoveredSlug(
        connector_name="nomatch:x", ats_family="nomatch", slug="x",
        company_name="X", discovered_at="2026-01-01T00:00:00+00:00",
        last_validated_at="2026-01-01T00:00:00+00:00",
        validation_status="no_match", consecutive_failures=0,
        last_posting_count=0, website=website, methods_tried=methods_tried,
    )


def test_exhausted_with_website_requires_fingerprint_tried():
    assert no_match_exhausted(_nm(website="https://x.com",
                                  methods_tried=["slug:x", "fingerprint"])) is True
    # fingerprint errored (transient) → not appended → NOT exhausted
    assert no_match_exhausted(_nm(website="https://x.com",
                                  methods_tried=["slug:x"])) is False


def test_exhausted_without_website_is_any_completed_chain():
    assert no_match_exhausted(_nm(website=None, methods_tried=["slug:x"])) is True


def test_legacy_rows_are_never_exhausted():
    assert no_match_exhausted(_nm(website=None, methods_tried=None)) is False
    assert no_match_exhausted(_nm(website="https://x.com", methods_tried=[])) is False


# ---------- candidate website (spec part 3: VC staging) ----------

from src.sqlite_db import connect
from src.state_sqlite import SqliteDiscoveredSlugsStore


def test_upsert_candidate_stores_website(store):
    store.upsert_candidate(
        "candidate:acme", company_name="Acme Inc.",
        website="acme.com", origin="vc:a16z",
    )
    row = store.get("candidate:acme")
    assert row.validation_status == "candidate"
    assert row.website == "acme.com"
    assert row.origin == "vc:a16z"
    assert row.company_name == "Acme Inc."


def test_upsert_candidate_website_defaults_none(store):
    store.upsert_candidate("greenhouse:newco", claimed_family="greenhouse")
    assert store.get("greenhouse:newco").website is None


def test_sqlite_upsert_candidate_stores_website():
    s = SqliteDiscoveredSlugsStore(connect(":memory:"))
    s.upsert_candidate("candidate:acme", company_name="Acme",
                       website="acme.com", origin="vc:a16z")
    row = s.get("candidate:acme")
    assert row.website == "acme.com" and row.origin == "vc:a16z"
