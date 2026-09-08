import boto3
import pytest
from moto import mock_aws

from src.models import ConnectorState
from src.state import SourceStateStore

TABLE = "source_state_test"


@pytest.fixture
def store():
    with mock_aws():
        ddb = boto3.client("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName=TABLE,
            AttributeDefinitions=[
                {"AttributeName": "connector_name", "AttributeType": "S"}
            ],
            KeySchema=[{"AttributeName": "connector_name", "KeyType": "HASH"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield SourceStateStore(table_name=TABLE)


def test_get_returns_default_state_when_missing(store):
    s = store.get("greenhouse:stripe")
    assert s == ConnectorState(etag=None, last_modified=None)


def test_get_many_returns_default_for_unknown(store):
    states = store.get_many(["greenhouse:stripe", "lever:foo"])
    assert states == {
        "greenhouse:stripe": ConnectorState(),
        "lever:foo": ConnectorState(),
    }


def test_put_then_get_roundtrips(store):
    store.put("greenhouse:stripe", ConnectorState(etag='W/"abc"', last_modified="Wed, 30 Apr 2026 18:00:00 GMT"))
    s = store.get("greenhouse:stripe")
    assert s.etag == 'W/"abc"'
    assert s.last_modified == "Wed, 30 Apr 2026 18:00:00 GMT"


def test_get_many_returns_stored_for_known_and_default_for_unknown(store):
    store.put("greenhouse:stripe", ConnectorState(etag='W/"abc"', last_modified=None))
    states = store.get_many(["greenhouse:stripe", "greenhouse:airbnb"])
    assert states["greenhouse:stripe"].etag == 'W/"abc"'
    assert states["greenhouse:airbnb"] == ConnectorState()


def test_put_overwrites(store):
    store.put("greenhouse:stripe", ConnectorState(etag="v1", last_modified=None))
    store.put("greenhouse:stripe", ConnectorState(etag="v2", last_modified=None))
    assert store.get("greenhouse:stripe").etag == "v2"


def test_get_many_chunks_over_100(store):
    keys = [f"greenhouse:co{i}" for i in range(150)]
    for k in keys[:50]:
        store.put(k, ConnectorState(etag=f"e-{k}", last_modified=None))
    states = store.get_many(keys)
    assert len(states) == 150
    assert states["greenhouse:co0"].etag == "e-greenhouse:co0"
    assert states["greenhouse:co100"] == ConnectorState()


def test_payload_roundtrips(store):
    store.put(
        "adzuna",
        ConnectorState(payload={"budget_date": "2026-07-18", "budget_calls": 3, "seen_ids": ["5001"]}),
    )
    s = store.get("adzuna")
    assert s.payload == {"budget_date": "2026-07-18", "budget_calls": 3, "seen_ids": ["5001"]}
    assert s.etag is None


def test_payload_absent_stays_none(store):
    store.put("greenhouse:stripe", ConnectorState(etag="v1", last_modified=None))
    assert store.get("greenhouse:stripe").payload is None
