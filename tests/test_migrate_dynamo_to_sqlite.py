# tests/test_migrate_dynamo_to_sqlite.py
import boto3
from moto import mock_aws

from src.sqlite_db import connect


def _make_tables(ddb):
    ddb.create_table(
        TableName="seen_jobs",
        KeySchema=[{"AttributeName": "job_id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "job_id", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    for t in ("source_state", "discovered_slugs", "connector_health"):
        ddb.create_table(
            TableName=t,
            KeySchema=[{"AttributeName": "connector_name", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "connector_name", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )


def test_migrate_copies_all_tables(tmp_path):
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        _make_tables(ddb)
        ddb.Table("seen_jobs").put_item(Item={
            "job_id": "j1", "first_seen": "2026-06-01T00:00:00+00:00",
            "notified": True, "title": "Staff Eng", "company": "Acme",
            "score": 9, "description_snapshot": "JD body",
        })
        ddb.Table("source_state").put_item(Item={"connector_name": "greenhouse:acme", "etag": "W/1"})
        ddb.Table("discovered_slugs").put_item(Item={
            "connector_name": "greenhouse:acme", "ats_family": "greenhouse", "slug": "acme",
            "discovered_at": "2026-06-01T00:00:00+00:00", "validation_status": "ok",
            "consecutive_failures": 0, "last_posting_count": 3,
        })
        ddb.Table("connector_health").put_item(Item={
            "connector_name": "lever:dead", "dead_streak": 4, "suppressed": True,
        })

        from scripts.migrate_dynamo_to_sqlite import migrate
        conn = connect(str(tmp_path / "out.db"))
        counts = migrate(region="us-east-1", dst_conn=conn)

        assert counts == {"seen_jobs": 1, "source_state": 1, "discovered_slugs": 1, "connector_health": 1}

        from src.state_sqlite import (
            SqliteConnectorHealthStore, SqliteDiscoveredSlugsStore,
            SqliteSeenJobsStore, SqliteSourceStateStore,
        )
        assert SqliteSeenJobsStore(conn).get_match("j1")["title"] == "Staff Eng"
        assert SqliteSourceStateStore(conn).get("greenhouse:acme").etag == "W/1"
        assert SqliteDiscoveredSlugsStore(conn).get("greenhouse:acme").last_posting_count == 3
        assert SqliteConnectorHealthStore(conn).suppressed_names() == {"lever:dead"}
