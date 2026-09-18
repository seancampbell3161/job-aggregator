"""The Companies page: what it lists and how it degrades."""
from src.web.app import create_app
from tests.auth_helpers import signed_in_client
from tests.settings_helpers import make_service

FULL = {"sources": {
    "greenhouse": ["acme"],
    "workday": [{"tenant": "microsoft", "region": "wd1", "site": "External"}],
    "avature": [{"careers_url": "https://careers.jacobs.com/en_US/careers/SearchJobs",
                 "company": "Jacobs"}],
}}


def _app(tmp_path, monkeypatch, service=None):
    monkeypatch.setenv("JOB_AGG_SQLITE_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("JOB_AGG_TAILORED_DIR", str(tmp_path / "tailored"))
    return create_app(service=service if service is not None else make_service(FULL))


def test_every_configured_board_is_listed(tmp_path, monkeypatch):
    r = signed_in_client(_app(tmp_path, monkeypatch)).get("/settings/companies")
    assert r.status_code == 200
    for text in ("acme", "microsoft", "Jacobs"):
        assert text in r.text
    assert "greenhouse:acme" in r.text
    assert "avature:jacobs" in r.text


def test_an_empty_instance_says_so_instead_of_rendering_an_empty_table(tmp_path, monkeypatch):
    r = signed_in_client(_app(tmp_path, monkeypatch, make_service({}))).get("/settings/companies")
    assert "No company boards yet" in r.text


def test_the_page_survives_a_health_store_failure(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(app.state.stores.health, "tracked_names", boom)
    r = signed_in_client(app).get("/settings/companies")
    assert r.status_code == 200
    assert "status unavailable" in r.text


def test_the_page_survives_a_discovered_store_failure(tmp_path, monkeypatch):
    """_discovery_only's own fail-soft path (companies.py), distinct from
    board_status's — a locked discovered_slugs table must not 500 the whole
    page, it should just mean no discovery-only nudge this time."""
    app = _app(tmp_path, monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(app.state.stores.discovered, "list_healthy", boom)
    r = signed_in_client(app).get("/settings/companies")
    assert r.status_code == 200
    assert "acme" in r.text  # the configured boards still render


def test_discovery_only_slugs_are_counted_not_listed(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    app.state.stores.discovered.upsert_ok("lever:discovered-co", last_posting_count=3)
    r = signed_in_client(app).get("/settings/companies")
    assert "discovered-co" not in r.text
    # base.html always links /pipeline from the global nav, so that alone
    # would pass even with the discovery-only nudge deleted outright — pin
    # the count into the assertion so a broken or missing count fails this.
    assert "1 more slug discovered but not yet added" in r.text


def test_no_discovery_only_nudge_when_nothing_is_pending(tmp_path, monkeypatch):
    r = signed_in_client(_app(tmp_path, monkeypatch)).get("/settings/companies")
    assert "discovered but not yet added" not in r.text
