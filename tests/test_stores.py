# tests/test_stores.py
from src.state_sqlite import SqliteRejectedPostingsStore, SqliteSeenJobsStore
from src.stores import Stores, build_stores


def test_build_stores_wires_sqlite_stores_over_one_connection(monkeypatch, tmp_path):
    monkeypatch.setenv("JOB_AGG_SQLITE_PATH", str(tmp_path / "t.db"))
    stores = build_stores()
    assert isinstance(stores, Stores)
    assert isinstance(stores.seen, SqliteSeenJobsStore)
    assert isinstance(stores.rejected, SqliteRejectedPostingsStore)
    assert stores.seen._conn is stores.source_state._conn


def test_build_stores_ignores_legacy_backend_env(monkeypatch, tmp_path):
    monkeypatch.setenv("JOB_AGG_SQLITE_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("JOB_AGG_BACKEND", "dynamodb")
    assert isinstance(build_stores().seen, SqliteSeenJobsStore)


def test_state_module_has_no_aws_dependency():
    import src.state as state
    assert not hasattr(state, "boto3")
    assert not hasattr(state, "SeenJobsStore")


def test_stores_requires_every_store():
    import dataclasses
    fields = dataclasses.fields(Stores)
    assert len(fields) == 10
    assert all(f.default is dataclasses.MISSING for f in fields)
