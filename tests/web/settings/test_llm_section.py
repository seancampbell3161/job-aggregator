"""The LLM section: settings plus secrets, and the Test button."""
import pytest

from src.web.app import create_app
from tests.auth_helpers import signed_in_client
from tests.settings_helpers import WEB_TEST_SETTINGS, make_service


def _app(tmp_path, monkeypatch, service=None):
    monkeypatch.setenv("JOB_AGG_SQLITE_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("JOB_AGG_TAILORED_DIR", str(tmp_path / "tailored"))
    return create_app(service=service if service is not None else make_service(WEB_TEST_SETTINGS))


FORM = {
    "relevance.enabled": "on",
    "relevance.provider": "anthropic",
    "relevance.model": "claude-haiku-4-5",
    "relevance.score_high": "7",
    "relevance.score_low": "4",
    "relevance.timeout_seconds": "10",
    "relevance.ollama_host": "http://ollama:11434",
    "gap_analysis.enabled": "on",
    "gap_analysis.provider": "",
    "gap_analysis.model": "",
    "tailoring.provider": "",
    "tailoring.model": "",
    "coach.provider": "",
    "coach.model": "",
}


def test_page_shows_secret_state_without_the_value(tmp_path, monkeypatch):
    service = make_service(WEB_TEST_SETTINGS, secrets={"anthropic_api_key": "sk-secret-value"})
    r = signed_in_client(_app(tmp_path, monkeypatch, service)).get("/settings/llm")
    assert r.status_code == 200
    assert "sk-secret-value" not in r.text
    assert "Saved" in r.text


def test_env_backed_secret_renders_disabled(tmp_path, monkeypatch):
    service = make_service(WEB_TEST_SETTINGS, env={"JOB_AGG_ANTHROPIC_API_KEY": "sk-env"})
    r = signed_in_client(_app(tmp_path, monkeypatch, service)).get("/settings/llm")
    assert "JOB_AGG_ANTHROPIC_API_KEY" in r.text
    assert "disabled" in r.text


def test_save_writes_settings_and_secrets(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    r = signed_in_client(app).post(
        "/settings/llm", data={**FORM, "secret.anthropic_api_key": "sk-new"})
    assert r.status_code == 200
    assert app.state.service.snapshot().cfg.relevance.score_low == 4
    assert app.state.service.secret_source("anthropic_api_key") == "stored"


def test_a_blank_secret_field_leaves_a_stored_value_alone(tmp_path, monkeypatch):
    service = make_service(WEB_TEST_SETTINGS, secrets={"anthropic_api_key": "sk-old"})
    app = _app(tmp_path, monkeypatch, service)
    signed_in_client(app).post("/settings/llm", data={**FORM, "secret.anthropic_api_key": ""})
    assert app.state.service.secret_source("anthropic_api_key") == "stored"


def test_the_clear_box_removes_a_stored_secret(tmp_path, monkeypatch):
    service = make_service(WEB_TEST_SETTINGS, secrets={"anthropic_api_key": "sk-old"})
    app = _app(tmp_path, monkeypatch, service)
    signed_in_client(app).post(
        "/settings/llm", data={**FORM, "clear.anthropic_api_key": "on"})
    assert app.state.service.secret_source("anthropic_api_key") == "unset"


def test_an_env_backed_secret_is_never_written(tmp_path, monkeypatch):
    service = make_service(WEB_TEST_SETTINGS, env={"JOB_AGG_ANTHROPIC_API_KEY": "sk-env"})
    app = _app(tmp_path, monkeypatch, service)
    signed_in_client(app).post(
        "/settings/llm", data={**FORM, "secret.anthropic_api_key": "sk-typed"})
    assert app.state.service.secret_source("anthropic_api_key") == "env"
    assert app.state.service._store.get_secret("anthropic_api_key") is None


def test_invalid_settings_block_the_secret_write(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    r = signed_in_client(app).post("/settings/llm", data={
        **FORM, "relevance.score_high": "99", "secret.anthropic_api_key": "sk-new"})
    assert r.status_code == 200
    assert "less than or equal to 10" in r.text
    assert app.state.service.secret_source("anthropic_api_key") == "unset"


def test_test_button_returns_a_partial(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)

    async def fake_probe(cfg, profile):
        from src.web.settings.probes import ProbeResult
        return ProbeResult(True, "Scored the sample posting 8/10")

    monkeypatch.setattr("src.web.settings.routes.probe_llm", fake_probe)
    r = signed_in_client(app).post("/settings/llm/test/llm", data=FORM)
    assert r.status_code == 200
    assert "8/10" in r.text
    assert "<html" not in r.text.lower()  # a partial, not a page


def test_test_button_honours_a_pending_clear(tmp_path, monkeypatch):
    """Tick "clear" on the API key and press Test: the probe must see the
    key as cleared (what Save will actually do to it), not the value still
    sitting in storage — otherwise Test reports green on a key that's about
    to be deleted the moment the user presses Save."""
    service = make_service(WEB_TEST_SETTINGS, secrets={"anthropic_api_key": "sk-old"})
    app = _app(tmp_path, monkeypatch, service)
    seen = {}

    async def fake_probe(cfg, profile):
        from src.web.settings.probes import ProbeResult
        seen["key"] = cfg.secrets.anthropic_api_key
        return ProbeResult(True, "ok")

    monkeypatch.setattr("src.web.settings.routes.probe_llm", fake_probe)
    signed_in_client(app).post(
        "/settings/llm/test/llm", data={**FORM, "clear.anthropic_api_key": "on"})
    assert seen["key"] == ""


def test_test_button_does_not_save(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    before = app.state.service.current_config()[0]

    async def fake_probe(cfg, profile):
        from src.web.settings.probes import ProbeResult
        return ProbeResult(True, "ok")

    monkeypatch.setattr("src.web.settings.routes.probe_llm", fake_probe)
    signed_in_client(app).post("/settings/llm/test/llm",
                               data={**FORM, "relevance.score_low": "9"})
    assert app.state.service.current_config()[0] == before
    assert app.state.service.snapshot().cfg.relevance.score_low == 4
