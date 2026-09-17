from datetime import datetime, timezone

import boto3
import pytest
from freezegun import freeze_time
from moto import mock_aws

from src.state import SeenJobsStore

TABLE = "seen_jobs_test"


@pytest.fixture
def store():
    with mock_aws():
        ddb = boto3.client("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName=TABLE,
            AttributeDefinitions=[{"AttributeName": "job_id", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "job_id", "KeyType": "HASH"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield SeenJobsStore(table_name=TABLE)


def test_diff_new_returns_all_when_table_empty(store):
    new_ids = store.diff_new(["greenhouse:stripe:1", "lever:foo:2"])
    assert set(new_ids) == {"greenhouse:stripe:1", "lever:foo:2"}


def test_mark_seen_then_diff_excludes(store):
    store.mark_seen("greenhouse:stripe:1", notified=True)
    new_ids = store.diff_new(["greenhouse:stripe:1", "greenhouse:stripe:2"])
    assert new_ids == ["greenhouse:stripe:2"]


def test_diff_new_chunks_over_100(store):
    seen = [f"src:c:{i}" for i in range(50)]
    for j in seen:
        store.mark_seen(j, notified=True)
    candidates = seen + [f"src:c:new{i}" for i in range(150)]
    new_ids = store.diff_new(candidates)
    assert len(new_ids) == 150
    assert all(j.startswith("src:c:new") for j in new_ids)


def test_mark_seen_writes_ttl(store):
    store.mark_seen("x:y:1", notified=True)
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    item = ddb.Table(TABLE).get_item(Key={"job_id": "x:y:1"})["Item"]
    assert item["notified"] is True
    assert int(item["ttl"]) > 0
    assert item["first_seen"]


def test_claim_for_notify_returns_true_when_unseen(store):
    assert store.claim_for_notify("g:s:1") is True


def test_claim_for_notify_returns_false_when_already_claimed(store):
    assert store.claim_for_notify("g:s:1") is True
    assert store.claim_for_notify("g:s:1") is False


def test_claim_for_notify_blocks_subsequent_diff_new(store):
    """A successful claim must make diff_new exclude the job_id, so a concurrent
    invocation can't re-emit it."""
    store.claim_for_notify("g:s:1")
    assert store.diff_new(["g:s:1", "g:s:2"]) == ["g:s:2"]


def test_release_claim_re_opens_diff_new(store):
    """If the notify path fully fails, releasing the claim must let a later
    invocation re-attempt the alert."""
    store.claim_for_notify("g:s:1")
    store.release_claim("g:s:1")
    assert "g:s:1" in store.diff_new(["g:s:1"])


def test_release_claim_idempotent_when_no_entry(store):
    """Releasing a claim that doesn't exist must not raise."""
    store.release_claim("g:s:never-claimed")


def test_claim_for_notify_persists_score_and_rationale(store):
    """A claim carrying a relevance score must store it so a high-scoring job
    can be explained after the fact."""
    assert store.claim_for_notify("g:s:1", score=8, rationale="Strong fit") is True
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    item = ddb.Table(TABLE).get_item(Key={"job_id": "g:s:1"})["Item"]
    assert int(item["score"]) == 8
    assert item["rationale"] == "Strong fit"


def test_claim_for_notify_omits_score_when_absent(store):
    """A claim with no score (relevance disabled / scoring not run) must still
    write a valid row and simply leave score/rationale off the item."""
    assert store.claim_for_notify("g:s:1") is True
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    item = ddb.Table(TABLE).get_item(Key={"job_id": "g:s:1"})["Item"]
    assert "score" not in item
    assert "rationale" not in item
    assert item["notified"] is True
    assert int(item["ttl"]) > 0


def test_claim_for_notify_persists_gaps(store):
    assert store.claim_for_notify("g:s:1", score=8, rationale="ok", gaps=["Kubernetes", "Kafka"]) is True
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    item = ddb.Table(TABLE).get_item(Key={"job_id": "g:s:1"})["Item"]
    assert item["gaps"] == ["Kubernetes", "Kafka"]


def test_claim_for_notify_omits_gaps_when_empty(store):
    assert store.claim_for_notify("g:s:1", gaps=[]) is True
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    item = ddb.Table(TABLE).get_item(Key={"job_id": "g:s:1"})["Item"]
    assert "gaps" not in item


def test_claim_for_notify_omits_gaps_when_none(store):
    assert store.claim_for_notify("g:s:1") is True
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    item = ddb.Table(TABLE).get_item(Key={"job_id": "g:s:1"})["Item"]
    assert "gaps" not in item


def test_recent_gap_lists_includes_window_and_excludes_old(store):
    with freeze_time("2026-06-01T00:00:00+00:00"):
        store.claim_for_notify("g:old:1", gaps=["Rust"])
    with freeze_time("2026-06-15T00:00:00+00:00"):
        store.claim_for_notify("g:new:1", gaps=["Kubernetes", "Kafka"])
        store.claim_for_notify("g:new:2")  # clean match: notified, no gaps

    since = datetime(2026, 6, 10, tzinfo=timezone.utc)
    lists = store.recent_gap_lists(since)

    assert ["Kubernetes", "Kafka"] in lists
    assert [] in lists                 # clean match contributes an empty list
    assert ["Rust"] not in lists       # outside the window
    assert len(lists) == 2             # honest denominator: 2 recent matches


def test_recent_gap_lists_empty_when_no_recent(store):
    since = datetime(2026, 6, 10, tzinfo=timezone.utc)
    assert store.recent_gap_lists(since) == []


def _posting(**over):
    from src.models import NormalizedPosting
    base = dict(
        job_id="g:s:1", title="Senior Backend Engineer", company="Stripe",
        location_text="Remote (US)", location_tags=frozenset({"remote"}),
        seniority="senior", stack=frozenset({"python"}),
        comp_min=180000, comp_max=220000, apply_url="https://jobs/1",
        description="big description we do NOT persist",
        posted_at=datetime(2026, 6, 16, tzinfo=timezone.utc), source="greenhouse:stripe",
    )
    base.update(over)
    return NormalizedPosting(**base)


def test_claim_for_notify_persists_display_fields(store):
    assert store.claim_for_notify("g:s:1", score=8, rationale="fit", posting=_posting()) is True
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    item = ddb.Table(TABLE).get_item(Key={"job_id": "g:s:1"})["Item"]
    assert item["title"] == "Senior Backend Engineer"
    assert item["company"] == "Stripe"
    assert item["location_text"] == "Remote (US)"
    assert int(item["comp_min"]) == 180000
    assert int(item["comp_max"]) == 220000
    assert item["apply_url"] == "https://jobs/1"
    assert item["source"] == "greenhouse:stripe"
    assert item["posted_at"].startswith("2026-06-16")
    assert "description" not in item  # never persisted


def test_claim_for_notify_omits_optional_display_fields_when_none(store):
    p = _posting(job_id="g:s:2", comp_min=None, comp_max=None, posted_at=None, apply_url="https://j/2")
    assert store.claim_for_notify("g:s:2", posting=p) is True
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    item = ddb.Table(TABLE).get_item(Key={"job_id": "g:s:2"})["Item"]
    assert item["title"] == "Senior Backend Engineer"
    assert "comp_min" not in item
    assert "comp_max" not in item
    assert "posted_at" not in item


def test_claim_for_notify_without_posting_writes_no_display_fields(store):
    assert store.claim_for_notify("g:s:3") is True
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    item = ddb.Table(TABLE).get_item(Key={"job_id": "g:s:3"})["Item"]
    assert "title" not in item
    assert "description_snapshot" not in item  # no posting → nothing to snapshot


def test_claim_for_notify_omits_description_snapshot_when_empty(store):
    p = _posting(job_id="g:s:4", description="")
    assert store.claim_for_notify("g:s:4", posting=p) is True
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    item = ddb.Table(TABLE).get_item(Key={"job_id": "g:s:4"})["Item"]
    assert item["title"] == "Senior Backend Engineer"  # posting still stored
    assert "description_snapshot" not in item          # empty description → field omitted


def test_list_matches_returns_only_notified_rows_with_title(store):
    # rich, notified
    store.claim_for_notify("g:s:1", score=8, rationale="fit", posting=_posting())
    # legacy notified row with no title (pre-migration) — must be skipped
    store._table.put_item(Item={"job_id": "g:s:legacy", "notified": True, "first_seen": "2026-06-16T00:00:00+00:00"})
    rows = store.list_matches()
    ids = {r["job_id"] for r in rows}
    assert ids == {"g:s:1"}
    row = rows[0]
    assert row["title"] == "Senior Backend Engineer"
    assert row["score"] == 8 and isinstance(row["score"], int)
    assert row["comp_min"] == 180000 and isinstance(row["comp_min"], int)
    assert row["status"] == "new"          # absent → default
    assert row["gaps"] == []               # absent → empty list
    assert row["first_seen"]


def test_list_matches_carries_status_and_gaps(store):
    store.claim_for_notify("g:s:1", score=7, rationale="ok",
                           gaps=["Kafka", "Kubernetes"], posting=_posting())
    store.set_status("g:s:1", "applied")
    rows = store.list_matches()
    assert rows[0]["status"] == "applied"
    assert rows[0]["gaps"] == ["Kafka", "Kubernetes"]


def test_get_match_returns_one_row_or_none(store):
    store.claim_for_notify("g:s:1", score=8, rationale="fit", posting=_posting())
    row = store.get_match("g:s:1")
    assert row["company"] == "Stripe"
    assert isinstance(row["score"], int)  # normalized through _to_match
    assert store.get_match("nope:0:0") is None


def test_get_match_skips_non_notified_row(store):
    """A non-notified row must not be surfaced by the detail lookup, mirroring
    list_matches (which filters on notified)."""
    store._table.put_item(Item={
        "job_id": "g:s:unnotified", "notified": False, "title": "Eng",
        "first_seen": "2026-06-16T00:00:00+00:00",
    })
    assert store.get_match("g:s:unnotified") is None


def test_set_status_kept_status_drops_ttl(store):
    store.claim_for_notify("g:s:1", score=8, rationale="fit", posting=_posting())
    assert store.set_status("g:s:1", "applied") is True
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    item = ddb.Table(TABLE).get_item(Key={"job_id": "g:s:1"})["Item"]
    assert item["status"] == "applied"
    assert "ttl" not in item  # kept status persists indefinitely


def test_set_status_dismissed_keeps_ttl(store):
    store.claim_for_notify("g:s:1", score=8, rationale="fit", posting=_posting())
    assert store.set_status("g:s:1", "dismissed") is True
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    item = ddb.Table(TABLE).get_item(Key={"job_id": "g:s:1"})["Item"]
    assert item["status"] == "dismissed"
    assert int(item["ttl"]) > 0  # still expires


def test_set_status_returns_false_for_missing_row(store):
    assert store.set_status("nope:0:0", "applied") is False


def test_set_status_rejects_invalid_status(store):
    store.claim_for_notify("g:s:1", score=8, rationale="fit", posting=_posting())
    with pytest.raises(ValueError):
        store.set_status("g:s:1", "bogus")


def test_set_status_dismiss_after_kept_leaves_ttl_absent(store):
    """Accepted edge case: once a kept status drops the TTL, dismissing the job
    does NOT re-add it — the row stays persistent. Intentional, documented here
    so nobody 'fixes' it."""
    store.claim_for_notify("g:s:1", score=8, rationale="fit", posting=_posting())
    store.set_status("g:s:1", "applied")    # drops TTL
    store.set_status("g:s:1", "dismissed")  # does NOT re-add it
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    item = ddb.Table(TABLE).get_item(Key={"job_id": "g:s:1"})["Item"]
    assert "ttl" not in item


def test_mark_suppressed_writes_notified_false_with_score(store):
    store.mark_suppressed("g:s:1", score=2)
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    item = ddb.Table(TABLE).get_item(Key={"job_id": "g:s:1"})["Item"]
    assert item["notified"] is False
    assert int(item["score"]) == 2
    assert "title" not in item          # minimal marker → invisible to list_matches
    assert int(item["ttl"]) > 0
    assert item["first_seen"]


def test_mark_suppressed_then_diff_excludes(store):
    """The whole point of the fix: a suppressed posting must be excluded by
    diff_new on later cycles so it is not re-fetched and re-scored."""
    store.mark_suppressed("g:s:1", score=2)
    assert store.diff_new(["g:s:1", "g:s:2"]) == ["g:s:2"]


def test_mark_suppressed_does_not_overwrite_existing_notified_row(store):
    """Concurrency safety: if a row already exists (e.g. a concurrent cycle
    already notified this job), the suppressed write must be a silent no-op so
    the notified row is never clobbered."""
    store.claim_for_notify("g:s:1", score=8, rationale="fit")
    store.mark_suppressed("g:s:1", score=2)
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    item = ddb.Table(TABLE).get_item(Key={"job_id": "g:s:1"})["Item"]
    assert item["notified"] is True     # unchanged
    assert int(item["score"]) == 8      # unchanged


def test_mark_suppressed_omits_score_when_none(store):
    store.mark_suppressed("g:s:1", score=None)
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    item = ddb.Table(TABLE).get_item(Key={"job_id": "g:s:1"})["Item"]
    assert "score" not in item
    assert item["notified"] is False


def test_list_suppressed_returns_only_suppressed_scored_rows(store):
    """list_suppressed returns notified=false rows that carry a score. Notified
    rows and score-less rows are excluded — it feeds only the histogram's low end."""
    store.mark_suppressed("g:s:low", score=2)                       # suppressed + scored → included
    store.claim_for_notify("g:s:hi", score=8, rationale="fit")      # notified → excluded
    store._table.put_item(Item={                                    # suppressed but no score → excluded
        "job_id": "g:s:noscore", "notified": False,
        "first_seen": "2026-06-16T00:00:00+00:00",
    })
    rows = store.list_suppressed()
    assert [r["score"] for r in rows] == [2]
    assert all(isinstance(r["score"], int) for r in rows)
    assert rows[0]["first_seen"]


HEALTH_TABLE = "connector_health_test"


@pytest.fixture
def health():
    with mock_aws():
        ddb = boto3.client("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName=HEALTH_TABLE,
            AttributeDefinitions=[{"AttributeName": "connector_name", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "connector_name", "KeyType": "HASH"}],
            BillingMode="PAY_PER_REQUEST",
        )
        from src.state import ConnectorHealthStore
        yield ConnectorHealthStore(table_name=HEALTH_TABLE)


def test_record_dead_increments_and_returns_streak(health):
    assert health.record_dead("greenhouse:acme") == 1
    assert health.record_dead("greenhouse:acme") == 2
    assert health.tracked_names() == {"greenhouse:acme"}
    assert health.suppressed_names() == set()


def test_mark_suppressed_shows_in_suppressed_and_tracked(health):
    health.record_dead("greenhouse:acme")
    health.mark_suppressed("greenhouse:acme")
    assert health.suppressed_names() == {"greenhouse:acme"}
    assert "greenhouse:acme" in health.tracked_names()


def test_clear_deletes_row_and_is_idempotent(health):
    health.record_dead("greenhouse:acme")
    health.clear("greenhouse:acme")
    assert health.tracked_names() == set()
    health.clear("greenhouse:acme")  # idempotent — must not raise


def test_tracked_vs_suppressed_distinguish_rows(health):
    health.record_dead("greenhouse:a")          # streak 1, not suppressed
    health.record_dead("greenhouse:b")
    health.mark_suppressed("greenhouse:b")        # suppressed
    assert health.tracked_names() == {"greenhouse:a", "greenhouse:b"}
    assert health.suppressed_names() == {"greenhouse:b"}


def test_claim_for_notify_stores_capped_description_snapshot():
    import boto3
    from moto import mock_aws
    from src.models import NormalizedPosting
    from src.state import SeenJobsStore

    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName="seen_jobs",
            KeySchema=[{"AttributeName": "job_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "job_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        store = SeenJobsStore("seen_jobs")
        posting = NormalizedPosting(
            job_id="greenhouse:stripe:1", title="SWE", company="Stripe",
            location_text="Remote, US", location_tags=frozenset({"remote", "us"}),
            seniority="senior", stack=frozenset({"go"}), comp_min=None, comp_max=None,
            apply_url="https://x/1", description="D" * 40000, posted_at=None, source="greenhouse:stripe",
        )
        assert store.claim_for_notify("greenhouse:stripe:1", posting=posting) is True
        item = ddb.Table("seen_jobs").get_item(Key={"job_id": "greenhouse:stripe:1"})["Item"]
        assert len(item["description_snapshot"]) == 30000      # capped
        assert item["description_snapshot"] == "D" * 30000


def test_set_status_accepts_new_outcome_statuses(store):
    store.mark_seen("g:s:1", notified=True)
    for s in ("offer", "rejected", "ghosted"):
        assert store.set_status("g:s:1", s) is True


def test_set_status_offer_drops_ttl(store):
    store.mark_seen("g:s:1", notified=True)
    assert store.set_status("g:s:1", "offer") is True
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    item = ddb.Table(TABLE).get_item(Key={"job_id": "g:s:1"})["Item"]
    assert "ttl" not in item


def test_set_status_rejected_and_ghosted_drop_ttl(store):
    for jid, s in (("g:s:2", "rejected"), ("g:s:3", "ghosted")):
        store.mark_seen(jid, notified=True)
        assert store.set_status(jid, s) is True
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        item = ddb.Table(TABLE).get_item(Key={"job_id": jid})["Item"]
        assert "ttl" not in item


def test_set_status_seeds_history_on_first_transition(store):
    store.mark_seen("g:s:1", notified=True)
    with freeze_time("2026-06-10T12:00:00Z"):
        store.set_status("g:s:1", "interested")
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    item = ddb.Table(TABLE).get_item(Key={"job_id": "g:s:1"})["Item"]
    assert item["history"] == [{"status": "interested", "at": "2026-06-10T12:00:00+00:00"}]


def test_set_status_appends_history_in_order(store):
    store.mark_seen("g:s:1", notified=True)
    with freeze_time("2026-06-10T12:00:00Z"):
        store.set_status("g:s:1", "interested")
    with freeze_time("2026-06-12T09:00:00Z"):
        store.set_status("g:s:1", "applied")
    item = (boto3.resource("dynamodb", region_name="us-east-1")
            .Table(TABLE).get_item(Key={"job_id": "g:s:1"})["Item"])
    assert [h["status"] for h in item["history"]] == ["interested", "applied"]
    assert item["history"][1]["at"] == "2026-06-12T09:00:00+00:00"


def test_list_matches_surfaces_history(store):
    store.claim_for_notify(
        "g:s:1", score=8, rationale="r", gaps=[],
        posting=_posting(),
    )
    with freeze_time("2026-06-10T12:00:00Z"):
        store.set_status("g:s:1", "applied")
    rows = store.list_matches()
    row = next(r for r in rows if r["job_id"] == "g:s:1")
    assert row["history"] == [{"status": "applied", "at": "2026-06-10T12:00:00+00:00"}]


def test_get_jd_reads_snapshot():
    import boto3
    from moto import mock_aws

    from src.models import NormalizedPosting
    from src.state import SeenJobsStore

    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName="seen_jobs",
            KeySchema=[{"AttributeName": "job_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "job_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        store = SeenJobsStore(table_name="seen_jobs", region="us-east-1")
        p = NormalizedPosting(
            job_id="j1", title="Staff Eng", company="Acme", location_text="Remote",
            location_tags=frozenset(), seniority="staff", stack=frozenset(),
            comp_min=None, comp_max=None, apply_url="https://x", description="JD body",
            posted_at=None, source="greenhouse:acme",
        )
        store.claim_for_notify("j1", posting=p)
        jd = store.get_jd("j1")
        assert jd.description == "JD body"
        assert jd.title == "Staff Eng"
        assert store.get_jd("missing") is None


def test_posting_display_fields_and_roundtrip():
    from src.models import NormalizedPosting
    from src.state import posting_display_fields, posting_from_item
    p = NormalizedPosting(
        job_id="greenhouse:acme:1", title="Backend Engineer", company="Acme",
        location_text="Remote (US)", location_tags=frozenset({"remote", "us"}),
        seniority="senior", stack=frozenset({"python"}),
        comp_min=150000, comp_max=200000, apply_url="https://a/1",
        description="x" * 40000,
        posted_at=datetime(2026, 7, 1, tzinfo=timezone.utc), source="greenhouse:acme",
    )
    fields = posting_display_fields(p)
    assert fields["title"] == "Backend Engineer"
    assert len(fields["description_snapshot"]) == 30000  # capped
    assert fields["posted_at"] == "2026-07-01T00:00:00+00:00"

    item = {"job_id": p.job_id, "location_tags": ["remote", "us"],
            "stack": ["python"], "seniority": "senior", **fields}
    back = posting_from_item(item)
    assert back.job_id == p.job_id
    assert back.title == p.title
    assert back.description == "x" * 30000
    assert back.location_tags == frozenset({"remote", "us"})
    assert back.stack == frozenset({"python"})
    assert back.comp_min == 150000
    assert back.posted_at is not None and back.posted_at.year == 2026


def test_posting_display_fields_persists_workplace_type_from_tags():
    from src.state import posting_display_fields
    # Flag-only remote role: the tags say remote (from the connector flag) but the
    # free text carries no "remote" marker — exactly the case the text-only fallback
    # gets wrong. The persisted value must come from the authoritative tags.
    remote = posting_display_fields(
        _posting(location_text="United States", location_tags=frozenset({"remote", "us"})))
    assert remote["workplace_type"] == "remote"
    hybrid = posting_display_fields(
        _posting(location_text="Denver, CO", location_tags=frozenset({"hybrid", "denver"})))
    assert hybrid["workplace_type"] == "hybrid"


def test_posting_display_fields_omits_workplace_type_without_tag_signal():
    from src.state import posting_display_fields
    # Geo tags only, no workplace signal → key omitted so the triage row falls back
    # to deriving from location_text.
    fields = posting_display_fields(_posting(location_tags=frozenset({"us", "seattle"})))
    assert "workplace_type" not in fields


def test_match_view_passes_workplace_type_through_and_defaults_none():
    from src.state import match_view
    assert match_view({"job_id": "x:1", "workplace_type": "hybrid"})["workplace_type"] == "hybrid"
    # legacy row without the field → None (triage falls back to text derivation)
    assert match_view({"job_id": "x:2"})["workplace_type"] is None


def test_posting_from_item_tolerates_sparse_item():
    from src.state import posting_from_item
    back = posting_from_item({"job_id": "x:1"})
    assert back.title == "" and back.company == ""
    assert back.description == ""
    assert back.location_tags == frozenset() and back.stack == frozenset()
    assert back.comp_min is None and back.posted_at is None
