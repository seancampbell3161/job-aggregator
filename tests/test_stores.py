# tests/test_stores.py
import pytest

from src.state_sqlite import SqliteSeenJobsStore
from src.stores import Stores, build_stores


def test_build_stores_sqlite_default(monkeypatch, tmp_path):
    monkeypatch.delenv("JOB_AGG_BACKEND", raising=False)
    monkeypatch.setenv("JOB_AGG_SQLITE_PATH", str(tmp_path / "t.db"))
    stores = build_stores()
    assert isinstance(stores, Stores)
    assert isinstance(stores.seen, SqliteSeenJobsStore)
    # all four share one connection
    assert stores.seen._conn is stores.source_state._conn


def test_build_stores_sqlite_explicit(monkeypatch, tmp_path):
    monkeypatch.setenv("JOB_AGG_BACKEND", "sqlite")
    monkeypatch.setenv("JOB_AGG_SQLITE_PATH", str(tmp_path / "t.db"))
    assert isinstance(build_stores().seen, SqliteSeenJobsStore)


def test_build_stores_dynamodb_selects_aws_classes(monkeypatch):
    monkeypatch.setenv("JOB_AGG_BACKEND", "dynamodb")
    from src.state import SeenJobsStore
    stores = build_stores()
    assert isinstance(stores.seen, SeenJobsStore)


def test_build_stores_unknown_backend_raises(monkeypatch):
    monkeypatch.setenv("JOB_AGG_BACKEND", "bogus")
    with pytest.raises(ValueError, match="JOB_AGG_BACKEND"):
        build_stores()


def test_sqlite_backend_wires_rejected_store(monkeypatch, tmp_path):
    monkeypatch.setenv("JOB_AGG_BACKEND", "sqlite")
    monkeypatch.setenv("JOB_AGG_SQLITE_PATH", str(tmp_path / "t.db"))
    from src.state_sqlite import SqliteRejectedPostingsStore
    from src.stores import build_stores
    stores = build_stores()
    assert isinstance(stores.rejected, SqliteRejectedPostingsStore)


def test_dynamodb_backend_has_no_rejected_store(monkeypatch):
    monkeypatch.setenv("JOB_AGG_BACKEND", "dynamodb")
    from src.stores import build_stores
    stores = build_stores()
    assert stores.rejected is None
    assert stores.alert_state is None
