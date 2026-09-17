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


def test_build_stores_ignores_legacy_backend_env(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("JOB_AGG_SQLITE_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("JOB_AGG_BACKEND", "dynamodb")
    with caplog.at_level("WARNING", logger="src.stores"):
        assert isinstance(build_stores().seen, SqliteSeenJobsStore)
    warnings = [r for r in caplog.records if r.message == "legacy_backend_env_ignored"]
    assert len(warnings) == 1
    assert warnings[0].value == "dynamodb"


def test_build_stores_no_warning_when_backend_env_unset_or_sqlite(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("JOB_AGG_SQLITE_PATH", str(tmp_path / "t.db"))
    monkeypatch.delenv("JOB_AGG_BACKEND", raising=False)
    with caplog.at_level("WARNING", logger="src.stores"):
        build_stores()
    assert not any(r.message == "legacy_backend_env_ignored" for r in caplog.records)

    monkeypatch.setenv("JOB_AGG_BACKEND", "sqlite")
    with caplog.at_level("WARNING", logger="src.stores"):
        build_stores()
    assert not any(r.message == "legacy_backend_env_ignored" for r in caplog.records)


def test_state_module_has_no_aws_dependency():
    import src.state as state
    assert not hasattr(state, "boto3")
    assert not hasattr(state, "SeenJobsStore")


def test_stores_requires_every_store():
    import dataclasses
    fields = dataclasses.fields(Stores)
    assert len(fields) == 12
    assert all(f.default is dataclasses.MISSING for f in fields)


def test_build_stores_gives_auth_its_own_connection(monkeypatch, tmp_path):
    from src.auth.store import SqliteAuthStore
    monkeypatch.setenv("JOB_AGG_SQLITE_PATH", str(tmp_path / "t.db"))
    stores = build_stores()
    assert isinstance(stores.auth, SqliteAuthStore)
    assert stores.auth._conn is not stores.seen._conn
    assert stores.auth._conn is not stores.settings._conn
