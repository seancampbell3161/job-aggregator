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


def test_no_warning_when_the_discovered_row_is_not_ok(tmp_path, monkeypatch):
    """Row presence alone isn't the signal — validation_status has to be
    "ok". A "failed" row (discovery tried this board and it's currently
    broken) is not discovery successfully tracking it, so it earns no
    warning either. This is the case that actually discriminates
    _discovered_ok_row's status check from a bare `is not None`."""
    app = _app(tmp_path, monkeypatch, {"sources": {"greenhouse": ["acme"]}})
    app.state.stores.discovered.upsert_failed("greenhouse:acme")
    r = signed_in_client(app).get(f"/settings/companies/remove/{_digest(app)}")
    assert "/settings/companies/block" not in r.text


def test_removing_a_digest_that_is_no_longer_configured_is_404(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, {"sources": {"greenhouse": ["acme"]}})
    r = signed_in_client(app).get("/settings/companies/remove/000000000000")
    assert r.status_code == 404


def test_the_entrys_own_company_wins_over_a_differing_discovered_name(tmp_path, monkeypatch):
    """The prefill order is entry.company, THEN the discovered row's
    company_name, THEN the label — in that order, not the reverse. This is
    the one place this task deviates from the brief's own snippet
    (`row.company_name or entry.label`), and it only shows up on a
    structured family whose element model actually has a company field
    (Avature, Workday, ...) — a slug family's entry.values never has one.
    Discovery's own company_name ("Jacobs Engineering LLC") differing from
    the configured entry's ("Jacobs") is what makes the two orderings
    disagree; if the precedence were ever swapped back to the brief's, this
    goes red."""
    doc = {"sources": {"avature": [
        {"careers_url": "https://careers.jacobs.com/en_US/careers/SearchJobs", "company": "Jacobs"},
    ]}}
    app = _app(tmp_path, monkeypatch, doc)
    app.state.stores.discovered.upsert_ok("avature:jacobs", company_name="Jacobs Engineering LLC")
    r = signed_in_client(app).get(f"/settings/companies/remove/{_digest(app, 'avature')}")
    assert 'value="Jacobs"' in r.text
    assert "Jacobs Engineering LLC" not in r.text


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
    r = signed_in_client(app).post(
        "/settings/companies/block", data={"company": "Acme Inc"}, follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/settings/companies?blocked=1"
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
