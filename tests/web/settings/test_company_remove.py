"""Removing a board, and the discovery trap it can leave behind."""
from src.settings.errors import StaleWrite
from src.web.app import create_app
from tests.auth_helpers import signed_in_client
from tests.settings_helpers import make_service


def _app(tmp_path, monkeypatch, doc):
    monkeypatch.setenv("JOB_AGG_SQLITE_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("JOB_AGG_TAILORED_DIR", str(tmp_path / "tailored"))
    return create_app(service=make_service(doc))


def _digest(app, family="greenhouse"):
    from src.settings.boards import board_entries
    entries = board_entries(app.state.service.snapshot().cfg)
    return next(e.digest for e in entries if e.family == family)


def test_the_confirm_names_the_board(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, {"sources": {"greenhouse": ["acme"]}})
    r = signed_in_client(app).get(f"/settings/companies/remove/{_digest(app)}")
    assert "greenhouse:acme" in r.text
    assert app.state.service.snapshot().cfg.sources.greenhouse == ["acme"]  # GET writes nothing


def test_the_confirm_warns_when_discovery_polls_the_same_board(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, {"sources": {"greenhouse": ["acme"]}})
    app.state.stores.discovered.upsert_ok("greenhouse:acme", company_name="Acme Inc")
    r = signed_in_client(app).get(f"/settings/companies/remove/{_digest(app)}")
    assert "discovery" in r.text.lower()
    assert "/settings/companies/block" in r.text
    assert 'value="Acme Inc"' in r.text  # prefilled and editable


def test_no_warning_when_discovery_has_not_seen_it(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, {"sources": {"greenhouse": ["acme"]}})
    r = signed_in_client(app).get(f"/settings/companies/remove/{_digest(app)}")
    assert "/settings/companies/block" not in r.text


def test_the_confirm_survives_a_discovered_store_failure(tmp_path, monkeypatch):
    """A locked discovered_slugs table must not 500 the confirm page — same
    fail-soft contract as board_status (src/web/settings/health.py): the page
    still renders, just without the discovery warning."""
    app = _app(tmp_path, monkeypatch, {"sources": {"greenhouse": ["acme"]}})

    def boom(*a, **k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(app.state.stores.discovered, "get", boom)
    r = signed_in_client(app).get(f"/settings/companies/remove/{_digest(app)}")
    assert r.status_code == 200
    assert "greenhouse:acme" in r.text
    assert "/settings/companies/block" not in r.text


def test_the_remove_form_posts_to_the_existing_generic_route(tmp_path, monkeypatch):
    """Ruling R8: exactly one code path removes a board. The confirm page may
    add a discovery warning and a block action, but its own Remove form must
    not invent a second removal route — it posts to Task 6's generic one."""
    app = _app(tmp_path, monkeypatch, {"sources": {"greenhouse": ["acme"]}})
    digest = _digest(app)
    r = signed_in_client(app).get(f"/settings/companies/remove/{digest}")
    assert f'action="/settings/rows/sources.greenhouse/{digest}/remove"' in r.text


def test_the_block_action_appends_to_blocked_companies(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, {"sources": {"greenhouse": ["acme"]}})
    signed_in_client(app).post("/settings/companies/block", data={"company": "Acme Inc"})
    assert app.state.service.snapshot().cfg.filters.blocked_companies == ["Acme Inc"]


def test_blocking_a_company_twice_writes_one_version(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch,
               {"filters": {"blocked_companies": ["Acme Inc"]}})
    before = len(app.state.service.versions(limit=None))
    signed_in_client(app).post("/settings/companies/block", data={"company": "Acme Inc"})
    assert len(app.state.service.versions(limit=None)) == before


def test_blocking_nothing_is_rejected(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, {"filters": {}})
    r = signed_in_client(app).post("/settings/companies/block", data={"company": "  "})
    assert r.status_code == 400
    assert app.state.service.snapshot().cfg.filters.blocked_companies == []


def test_a_stale_write_while_blocking_is_reported_not_500(tmp_path, monkeypatch):
    """Every other write path in this module reports a concurrent save
    instead of letting it 500 (see test_rows.py's own StaleWrite coverage) —
    the brief's block route omitted this, so nail it down here."""
    app = _app(tmp_path, monkeypatch, {"filters": {}})

    def boom(mutate, *, source):
        raise StaleWrite("conflict")

    monkeypatch.setattr(app.state.service, "update_settings", boom)
    r = signed_in_client(app).post("/settings/companies/block", data={"company": "Acme Inc"})
    assert r.status_code == 409
