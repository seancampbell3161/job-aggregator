from src.starter_pack import PackSlug, StarterPack
from src.web.app import create_app
from tests.auth_helpers import signed_in_client
from tests.settings_helpers import WEB_TEST_SETTINGS, make_service

PACK = StarterPack("t", tuple(PackSlug("lever", f"c{i}", None, "us", 1) for i in range(12)), ())


def _app(tmp_path, monkeypatch, doc=None):
    monkeypatch.setenv("JOB_AGG_SQLITE_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("JOB_AGG_TAILORED_DIR", str(tmp_path / "tailored"))
    monkeypatch.setattr("src.web.settings.companies.default_pack", lambda: PACK)
    return create_app(service=make_service(doc or WEB_TEST_SETTINGS))


def test_banner_offers_pack_when_off(tmp_path, monkeypatch):
    r = signed_in_client(_app(tmp_path, monkeypatch)).get("/settings/companies")
    assert "Add 12 verified tech-company boards?" in r.text
    assert 'action="/settings/companies/starter-pack"' in r.text


def test_add_turns_pack_on_and_hides_banner(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    client = signed_in_client(app)
    r = client.post("/settings/companies/starter-pack", follow_redirects=False)
    assert r.headers["location"] == "/settings/companies?added=1"
    assert app.state.service.snapshot().cfg.discovery.starter_pack is True
    assert "verified tech-company boards?" not in client.get("/settings/companies").text


def test_dismiss_hides_banner_without_changing_settings(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    client = signed_in_client(app)
    client.post("/settings/companies/starter-pack/dismiss")
    assert app.state.service.snapshot().cfg.discovery.starter_pack is False
    assert "verified tech-company boards?" not in client.get("/settings/companies").text


def test_no_banner_when_pack_already_on(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, {**WEB_TEST_SETTINGS, "discovery": {"starter_pack": True}})
    assert "verified tech-company boards?" not in signed_in_client(app).get("/settings/companies").text


def test_no_banner_when_pack_is_empty(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    monkeypatch.setattr("src.web.settings.companies.default_pack", lambda: StarterPack("", (), ()))
    assert "verified tech-company boards?" not in signed_in_client(app).get("/settings/companies").text
