"""The /settings shell: nav, per-request snapshot rendering, Overview."""
from src.web.app import create_app
from tests.auth_helpers import signed_in_client
from tests.settings_helpers import WEB_TEST_SETTINGS, make_service


def _client(tmp_path, monkeypatch, service=None, **kw):
    monkeypatch.setenv("JOB_AGG_SQLITE_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("JOB_AGG_TAILORED_DIR", str(tmp_path / "tailored"))
    app = create_app(service=service if service is not None else make_service(WEB_TEST_SETTINGS))
    return signed_in_client(app, **kw)


def test_settings_redirects_to_overview(tmp_path, monkeypatch):
    r = _client(tmp_path, monkeypatch, follow_redirects=False).get("/settings")
    assert r.status_code == 303
    assert r.headers["location"] == "/settings/overview"


def test_overview_lists_every_section_in_the_sidebar(tmp_path, monkeypatch):
    from src.web.settings.sections import SECTIONS
    r = _client(tmp_path, monkeypatch).get("/settings/overview")
    assert r.status_code == 200
    for s in SECTIONS:
        assert f'href="/settings/{s.slug}"' in r.text


def test_overview_shows_readiness_warnings(tmp_path, monkeypatch):
    r = _client(tmp_path, monkeypatch).get("/settings/overview")
    assert "no posting can ever match" in r.text
    assert "never delivered anywhere" in r.text


def test_overview_warnings_disappear_when_configured(tmp_path, monkeypatch):
    service = make_service(
        {"filters": {"titles": ["platform engineer"], "max_age_days": 2},
         "sources": {"greenhouse": ["stripe"]}},
        secrets={"ntfy_topic_url": "https://ntfy.sh/x"},
    )
    r = _client(tmp_path, monkeypatch, service).get("/settings/overview")
    assert "no posting can ever match" not in r.text
    assert "never delivered anywhere" not in r.text


def test_overview_reports_the_version_in_effect(tmp_path, monkeypatch):
    r = _client(tmp_path, monkeypatch).get("/settings/overview")
    assert "Version" in r.text
    assert "apply live" in r.text  # the no-restart promise


def test_unknown_section_is_404(tmp_path, monkeypatch):
    assert _client(tmp_path, monkeypatch).get("/settings/nope").status_code == 404


def test_settings_needs_a_session(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setenv("JOB_AGG_SQLITE_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("JOB_AGG_TAILORED_DIR", str(tmp_path / "tailored"))
    app = create_app(service=make_service(WEB_TEST_SETTINGS))
    # A fresh AuthService has no claimed password yet, so an anonymous request
    # would land on /welcome (see test_without_a_password_everything_goes_to_welcome
    # in tests/web/test_auth_gate.py) rather than exercising the session gate
    # this test is actually about — claim one first, same as that suite's _app().
    app.state.auth.set_password("correct horse")
    r = TestClient(app, follow_redirects=False).get("/settings/overview")
    assert r.status_code == 303
    assert r.headers["location"].startswith("/login")


def test_nav_links_to_settings(tmp_path, monkeypatch):
    assert 'href="/settings"' in _client(tmp_path, monkeypatch).get("/").text
