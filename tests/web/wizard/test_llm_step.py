"""Connecting an LLM: the same write path and the same probe as /settings/llm."""
import pytest

from src.web.app import create_app
from src.web.settings.probes import ProbeResult
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
    "relevance.ollama_host": "http://ollama:11434",
    "secret.anthropic_api_key": "sk-test",
}


def test_saves_provider_and_key_then_advances(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    r = signed_in_client(app).post("/wizard/llm", data=FORM, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/wizard"
    cfg = app.state.service.snapshot().cfg
    assert cfg.relevance.enabled is True
    assert cfg.relevance.provider == "anthropic"
    assert app.state.service.effective_secret("anthropic_api_key") == "sk-test"


def test_the_version_is_sourced_to_the_wizard(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    signed_in_client(app).post("/wizard/llm", data=FORM)
    assert app.state.service.versions(1)[0].source == "wizard"


def test_a_bad_value_re_renders_with_what_was_typed(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    bad = {**FORM, "relevance.timeout_seconds": "not-a-number"}
    r = signed_in_client(app).post("/wizard/llm", data=bad)
    assert r.status_code == 200
    assert "whole number" in r.text
    assert "claude-haiku-4-5" in r.text  # nothing typed was lost


def test_a_bad_value_does_not_write_a_version(tmp_path, monkeypatch):
    """Mutation guard for the above: a validation failure must leave the
    settings store untouched, not merely render an error on top of a write."""
    app = _app(tmp_path, monkeypatch)
    before = len(app.state.service.versions(None))
    bad = {**FORM, "relevance.timeout_seconds": "not-a-number"}
    signed_in_client(app).post("/wizard/llm", data=bad)
    assert len(app.state.service.versions(None)) == before
    assert app.state.service.secret_source("anthropic_api_key") == "unset"


def test_resubmitting_unchanged_values_writes_no_new_version(tmp_path, monkeypatch):
    """save_step must filter the decoded patch against the config already in
    effect (like save_section does) — otherwise every resubmission of an
    already-saved step bumps the settings version even though nothing moved."""
    app = _app(tmp_path, monkeypatch)
    client = signed_in_client(app)
    client.post("/wizard/llm", data=FORM)
    before = len(app.state.service.versions(None))
    r = client.post("/wizard/llm", data=FORM, follow_redirects=False)
    assert r.status_code == 303
    assert len(app.state.service.versions(None)) == before


def test_test_button_runs_the_real_probe(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)

    async def fake(cfg, profile):
        return ProbeResult(True, "Scored the sample posting 8/10 — looks right.")

    monkeypatch.setattr("src.web.wizard.routes.probe_llm", fake)
    r = signed_in_client(app).post("/wizard/llm/test", data=FORM)
    assert r.status_code == 200
    assert "8/10" in r.text


def test_test_button_reports_failure(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)

    async def fake(cfg, profile):
        return ProbeResult(False, "401 unauthorized")

    monkeypatch.setattr("src.web.wizard.routes.probe_llm", fake)
    r = signed_in_client(app).post("/wizard/llm/test", data=FORM)
    assert "401 unauthorized" in r.text


def test_test_button_persists_nothing(tmp_path, monkeypatch):
    """A probe tests the values in the form — same contract as Settings."""
    app = _app(tmp_path, monkeypatch)

    async def fake(cfg, profile):
        return ProbeResult(True, "ok")

    monkeypatch.setattr("src.web.wizard.routes.probe_llm", fake)
    before = len(app.state.service.versions(None))
    signed_in_client(app).post("/wizard/llm/test", data=FORM)
    assert len(app.state.service.versions(None)) == before
    assert app.state.service.secret_source("anthropic_api_key") == "unset"


def test_test_button_passes_the_typed_key_to_the_probe(tmp_path, monkeypatch):
    """Mutation guard: a probe that ignores the typed (unsaved) secret and
    scores against whatever is already stored would still pass the two tests
    above. Assert the value the fake probe actually received."""
    app = _app(tmp_path, monkeypatch)
    seen = {}

    async def fake(cfg, profile):
        seen["key"] = cfg.secrets.anthropic_api_key
        return ProbeResult(True, "ok")

    monkeypatch.setattr("src.web.wizard.routes.probe_llm", fake)
    signed_in_client(app).post("/wizard/llm/test", data=FORM)
    assert seen["key"] == "sk-test"


def test_test_button_honours_a_pending_clear(tmp_path, monkeypatch):
    """Ticking clear then testing must not probe with the key Save will
    delete. Reachable in the wizard, not just in Settings: save the LLM step
    once (the key becomes "stored"), GET /wizard/llm again (no guard against
    revisiting a completed step), tick clear, press Test — same
    secret_field macro and secret_rows() helper Settings uses."""
    app = _app(tmp_path, monkeypatch)
    app.state.service.set_secret("anthropic_api_key", "sk-stored")
    seen = {}

    async def fake(cfg, profile):
        seen["key"] = cfg.secrets.anthropic_api_key
        return ProbeResult(True, "ok")

    monkeypatch.setattr("src.web.wizard.routes.probe_llm", fake)
    signed_in_client(app).post("/wizard/llm/test", data={
        **FORM, "secret.anthropic_api_key": "", "clear.anthropic_api_key": "on",
    })
    assert seen["key"] == ""
