import boto3
import pytest
from moto import mock_aws

from src.tailor.endpoint.jd import read_jd, PostingJD

TABLE = "seen_jobs"


def _make_table():
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    ddb.create_table(
        TableName=TABLE,
        KeySchema=[{"AttributeName": "job_id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "job_id", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    return ddb.Table(TABLE)


@mock_aws
def test_read_jd_returns_posting_when_snapshot_present():
    t = _make_table()
    t.put_item(Item={"job_id": "j1", "description_snapshot": "Build stuff. Need Go.",
                     "title": "SWE", "company": "Acme"})
    jd = read_jd(TABLE, "j1")
    assert jd == PostingJD(description="Build stuff. Need Go.", title="SWE", company="Acme")


@mock_aws
def test_read_jd_none_when_snapshot_absent():
    t = _make_table()
    t.put_item(Item={"job_id": "j2", "title": "SWE"})  # no description_snapshot
    assert read_jd(TABLE, "j2") is None


@mock_aws
def test_read_jd_none_when_missing_row():
    _make_table()
    assert read_jd(TABLE, "nope") is None
