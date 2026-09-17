def test_create_app_shares_sqlite_stores(monkeypatch, tmp_path):
    monkeypatch.setenv("JOB_AGG_SQLITE_PATH", str(tmp_path / "t.db"))
    from src.state_sqlite import SqliteSeenJobsStore
    from src.web.app import create_app
    app = create_app()
    assert isinstance(app.state.stores.seen, SqliteSeenJobsStore)
    # repo reads from the same backend (empty DB -> empty list)
    assert app.state.repo.list() == []
