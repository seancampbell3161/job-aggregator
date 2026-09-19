"""The résumé-drafting feature's own provider/model/timeout overrides."""
from src.config import AppConfig


def test_defaults_fall_back_to_relevance():
    from src.llm.providers import resolve
    cfg = AppConfig(relevance={"provider": "gemini", "model": "g"})
    assert resolve(cfg, "resume_draft") == ("gemini", "g")


def test_override_is_honoured():
    from src.llm.providers import resolve
    cfg = AppConfig(
        relevance={"provider": "gemini", "model": "g"},
        resume_draft={"provider": "anthropic", "model": "c"},
    )
    assert resolve(cfg, "resume_draft") == ("anthropic", "c")


def test_timeout_default():
    assert AppConfig().resume_draft.timeout_seconds == 60


def test_there_is_no_enabled_flag():
    """Drafting degrades to manual forms; it is not switched off."""
    assert "enabled" not in AppConfig().resume_draft.model_fields


def test_every_new_path_is_claimed_by_a_section():
    from src.web.settings.sections import CLAIMED_PATHS
    for path in ("resume_draft.provider", "resume_draft.model", "resume_draft.timeout_seconds"):
        assert path in CLAIMED_PATHS


def test_config_md_documents_every_new_flag():
    from pathlib import Path
    text = Path("docs/CONFIG.md").read_text()
    for path in ("resume_draft.provider", "resume_draft.model", "resume_draft.timeout_seconds"):
        assert path in text
