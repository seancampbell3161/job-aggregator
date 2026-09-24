"""Every secret's field-meta shows a plain word for its storage state, never
the internal `secret_source()` value ("unset" / "stored" / "env")."""
from src.web.app import create_app
from tests.auth_helpers import signed_in_client
from tests.settings_helpers import WEB_TEST_SETTINGS, make_service


def _app(tmp_path, monkeypatch, service=None):
    monkeypatch.setenv("JOB_AGG_SQLITE_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("JOB_AGG_TAILORED_DIR", str(tmp_path / "tailored"))
    return create_app(service=service if service is not None else make_service(WEB_TEST_SETTINGS))


def _assert_no_raw_source_meta(text: str) -> None:
    assert 'field-meta">unset' not in text
    assert 'field-meta">stored' not in text
    assert 'field-meta">env' not in text


def test_llm_page_secret_meta_is_plain_words(tmp_path, monkeypatch):
    service = make_service(
        WEB_TEST_SETTINGS,
        secrets={"anthropic_api_key": "sk-secret"},
        env={"JOB_AGG_GOOGLE_API_KEY": "sk-env"},
    )
    r = signed_in_client(_app(tmp_path, monkeypatch, service)).get("/settings/llm")
    assert r.status_code == 200
    _assert_no_raw_source_meta(r.text)
    assert "Not set" in r.text  # ollama_api_key is unset
    assert "Saved" in r.text  # anthropic_api_key is stored
    assert "Set outside the app" in r.text  # google_api_key is env-backed


def test_notifications_page_secret_meta_is_plain_words(tmp_path, monkeypatch):
    r = signed_in_client(_app(tmp_path, monkeypatch)).get("/settings/notifications")
    assert r.status_code == 200
    _assert_no_raw_source_meta(r.text)
    assert "Not set" in r.text


def test_integrations_page_secret_meta_is_plain_words(tmp_path, monkeypatch):
    service = make_service(WEB_TEST_SETTINGS, secrets={"adzuna_app_id": "abc123"})
    r = signed_in_client(_app(tmp_path, monkeypatch, service)).get("/settings/integrations")
    assert r.status_code == 200
    _assert_no_raw_source_meta(r.text)
    assert "Not set" in r.text
    assert "Saved" in r.text
