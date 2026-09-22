"""The LLM presets are data, so they can be checked: every recipe must be one
the app would actually accept, and its instructions must still match the docs
they were quoted from."""
import pathlib

import pytest

from src.config import AppConfig, Secrets
from src.settings.service import canonical_doc
from src.settings.patch import apply_patch
from src.web.app import create_app
from src.web.wizard.presets import LLM_PRESETS, preset_for_provider
from src.web.wizard.routes import LLM_PATHS
from tests.auth_helpers import signed_in_client
from tests.settings_helpers import WEB_TEST_SETTINGS, make_service

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]


def _app(tmp_path, monkeypatch):
    monkeypatch.setenv("JOB_AGG_SQLITE_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("JOB_AGG_TAILORED_DIR", str(tmp_path / "tailored"))
    return create_app(service=make_service(WEB_TEST_SETTINGS))


@pytest.mark.parametrize("preset", LLM_PRESETS, ids=lambda p: p.key)
def test_every_preset_is_a_config_the_app_accepts(preset):
    """A preset fills the form with these values, so the save that follows
    must succeed — a typo'd provider or a renamed path would otherwise only
    show up as a 500 when a user clicks the button."""
    doc = canonical_doc(AppConfig())
    apply_patch(doc, preset.values)
    AppConfig.model_validate(doc)


@pytest.mark.parametrize("preset", LLM_PRESETS, ids=lambda p: p.key)
def test_every_preset_only_sets_fields_this_step_owns(preset):
    """The step posts LLM_PATHS; a value outside that set would be silently
    dropped on save, leaving the form showing something it never stored."""
    assert set(preset.values) <= set(LLM_PATHS)


@pytest.mark.parametrize("preset", LLM_PRESETS, ids=lambda p: p.key)
def test_a_preset_names_a_real_secret_or_none(preset):
    assert preset.secret is None or preset.secret in Secrets.model_fields


@pytest.mark.parametrize("preset", LLM_PRESETS, ids=lambda p: p.key)
def test_no_preset_carries_a_secret_value(preset):
    """Presets say which key a route needs; they never supply one."""
    assert not any("key" in path.split(".")[-1] for path in preset.values)


@pytest.mark.parametrize(
    "preset", [p for p in LLM_PRESETS if p.key_url], ids=lambda p: p.key)
def test_preset_key_urls_still_match_getting_started(preset):
    """Drift guard. These URLs and cost lines are quoted from
    GETTING_STARTED.md's provider table; if that table moves on, the wizard
    would keep sending users somewhere stale. Fail loudly instead."""
    docs = (REPO_ROOT / "GETTING_STARTED.md").read_text()
    assert preset.key_url in docs, (
        f"{preset.key}: {preset.key_url} is no longer in GETTING_STARTED.md — "
        "update the preset and the docs together"
    )


def test_every_preset_sets_the_same_fields():
    """A recipe is the whole combination, so each must set every field any
    other one sets. A preset that leaves a field alone inherits whatever the
    previously-clicked recipe put there — click Ollama Cloud then Anthropic and
    the form keeps ollama_host=https://ollama.com, which then gets saved."""
    field_sets = {p.key: set(p.values) for p in LLM_PRESETS}
    expected = set().union(*field_sets.values())
    for key, fields in field_sets.items():
        assert fields == expected, (
            f"{key} leaves {sorted(expected - fields)} at whatever the last "
            "recipe set"
        )


def test_the_local_preset_needs_no_key_and_says_how_to_start_ollama():
    """The one route whose prerequisite isn't "go get a key". The bundled
    service is profile-gated, so without this the user is left with a filled
    form pointing at a container that isn't running."""
    local = next(p for p in LLM_PRESETS if p.key == "ollama_local")
    assert local.secret is None
    assert any("--profile ollama" in c for c in local.commands)


def test_the_opening_panel_follows_the_configured_provider():
    assert preset_for_provider("ollama", "http://ollama:11434") == "ollama_local"
    assert preset_for_provider("ollama", "https://ollama.com") == "ollama_cloud"
    assert preset_for_provider("anthropic", "") == "anthropic"
    assert preset_for_provider("gemini", "") == "gemini"


def test_the_llm_step_renders_every_preset(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    r = signed_in_client(app).get("/wizard/llm")
    assert r.status_code == 200
    for preset in LLM_PRESETS:
        assert preset.label in r.text
        for step in preset.steps:
            assert step in r.text


def test_the_llm_step_offers_the_local_recipe_commands(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    r = signed_in_client(app).get("/wizard/llm")
    assert "docker compose --profile ollama up -d" in r.text


def test_the_ollama_cloud_recipe_mentions_its_free_tier():
    """Cost is the line people choose on, and "flat monthly subscription"
    alone reads as "card required" — the free tier covers ordinary ranking
    and tailoring, which is most of what this app does."""
    cloud = next(p for p in LLM_PRESETS if p.key == "ollama_cloud")
    assert "free" in cloud.cost.lower()
    assert "free" in cloud.summary.lower()
