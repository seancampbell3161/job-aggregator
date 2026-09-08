# tests/test_state_sqlite_source.py
from src.models import ConnectorState
from src.sqlite_db import connect
from src.state_sqlite import SqliteSourceStateStore


def _store():
    return SqliteSourceStateStore(connect(":memory:"))


def test_get_missing_returns_empty_state():
    st = _store().get("greenhouse:acme")
    assert st == ConnectorState()


def test_put_then_get_roundtrips():
    s = _store()
    s.put("greenhouse:acme", ConnectorState(etag="W/abc", last_modified="Mon"))
    st = s.get("greenhouse:acme")
    assert st.etag == "W/abc"
    assert st.last_modified == "Mon"


def test_get_many_mixes_present_and_absent():
    s = _store()
    s.put("a", ConnectorState(etag="1"))
    out = s.get_many(["a", "b"])
    assert out["a"].etag == "1"
    assert out["b"] == ConnectorState()


def test_payload_roundtrips_sqlite(tmp_path):
    from src.models import ConnectorState
    from src.sqlite_db import connect
    from src.state_sqlite import SqliteSourceStateStore

    store = SqliteSourceStateStore(connect(str(tmp_path / "t.db")))
    store.put(
        "adzuna",
        ConnectorState(payload={"budget_date": "2026-07-18", "budget_calls": 3, "seen_ids": ["5001"]}),
    )
    s = store.get("adzuna")
    assert s.payload == {"budget_date": "2026-07-18", "budget_calls": 3, "seen_ids": ["5001"]}
    assert s.etag is None


def test_payload_absent_stays_none_sqlite(tmp_path):
    from src.models import ConnectorState
    from src.sqlite_db import connect
    from src.state_sqlite import SqliteSourceStateStore

    store = SqliteSourceStateStore(connect(str(tmp_path / "t.db")))
    store.put("greenhouse:stripe", ConnectorState(etag="v1", last_modified=None))
    assert store.get("greenhouse:stripe").payload is None
