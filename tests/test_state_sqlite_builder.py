from src.sqlite_db import connect
from src.state_sqlite import SqliteBuilderSettingsStore
from src.stores import build_stores


def _store(tmp_path, monkeypatch):
    monkeypatch.setenv("JOB_AGG_SQLITE_PATH", str(tmp_path / "t.db"))
    return SqliteBuilderSettingsStore(connect())


def test_get_returns_empty_dict_when_unset(tmp_path, monkeypatch):
    assert _store(tmp_path, monkeypatch).get() == {}


def test_put_then_get_roundtrip(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    store.put({"active_template": "headless", "max_pages": 2})
    assert store.get() == {"active_template": "headless", "max_pages": 2}


def test_put_overwrites_previous(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    store.put({"max_pages": 2})
    store.put({"max_pages": 3})
    assert store.get() == {"max_pages": 3}


def test_build_stores_wires_builder(tmp_path, monkeypatch):
    monkeypatch.setenv("JOB_AGG_BACKEND", "sqlite")
    monkeypatch.setenv("JOB_AGG_SQLITE_PATH", str(tmp_path / "t.db"))
    stores = build_stores()
    assert stores.builder is not None
    stores.builder.put({"max_pages": 2})
    assert stores.builder.get()["max_pages"] == 2
