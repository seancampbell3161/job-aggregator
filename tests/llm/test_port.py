"""The properties the port must not break."""
import inspect

import pytest

from src.config import AppConfig, Secrets


def test_probe_still_imports_the_pollers_factory():
    """probes.py imports handler's private factory deliberately, so the Test
    button and the poller can never score a posting differently. A port that
    renamed it would break that guarantee silently."""
    import src.web.settings.probes as probes
    from src.handler import _build_relevance_scorer
    assert probes._build_scorer is _build_relevance_scorer


def test_readiness_uses_the_shared_provider_map():
    """readiness.py held a fifth copy of provider -> secret."""
    from src.llm.providers import PROVIDER_KEYS
    from src.web.settings import readiness
    assert readiness._PROVIDER_KEYS is PROVIDER_KEYS


def test_every_factory_routes_through_build_binding(monkeypatch):
    """All five construct their client the same way now. Each is called with
    settings that should yield a binding; if any still hand-rolls its client,
    it won't appear in `seen`."""
    import src.llm.providers as providers

    seen: list[str] = []
    real = providers.build_binding

    def spy(cfg, *, feature, timeout_seconds):
        seen.append(feature)
        return real(cfg, feature=feature, timeout_seconds=timeout_seconds)

    monkeypatch.setattr(providers, "build_binding", spy)
    for module in ("src.handler", "src.tailor", "src.tailor.render.docx_import"):
        monkeypatch.setattr(f"{module}.build_binding", spy, raising=False)

    cfg = AppConfig(
        relevance={"enabled": True, "provider": "ollama", "model": "m",
                   "ollama_host": "http://ollama:11434"},
        gap_analysis={"enabled": True},
        coach={"enabled": True},
        tailoring={"enabled": True},
        secrets=Secrets(),
    )

    from src.handler import _build_coach, _build_gap_analyzer, _build_relevance_scorer
    _build_relevance_scorer(cfg, "profile text")
    _build_gap_analyzer(cfg, "resume text")
    _build_coach(cfg)

    from src.tailor import build_tailor_engine
    from src.tailor.models import EvidenceBank, ResumeContent
    build_tailor_engine(
        cfg,
        ResumeContent(name="A", contact={}, skills=[], experiences=[]),
        EvidenceBank(),
    )

    from src.tailor.render.docx_import import build_docx_importer
    build_docx_importer(cfg)

    assert set(seen) == {"relevance", "gap_analysis", "coach", "tailoring"}
    assert seen.count("tailoring") == 2  # engine + docx importer share the section


def test_relevance_scorer_with_malformed_host_and_no_profile_returns_none():
    """Regression: build_binding constructs a real client as part of
    answering "is this feature available?" — ollama.AsyncClient's
    constructor raises ValueError on a malformed host (confirmed directly
    below). A factory must refuse on its cheap checks (missing_key, then the
    missing-document check) *before* ever calling build_binding, so a
    malformed ollama_host on a first run with no profile saved yet degrades
    to None instead of blowing up the whole poll cycle."""
    from ollama import AsyncClient
    with pytest.raises(ValueError):
        AsyncClient(host="not a valid url :::: at all")

    from src.handler import _build_relevance_scorer
    cfg = AppConfig(
        relevance={"enabled": True, "provider": "ollama", "model": "m",
                   "ollama_host": "not a valid url :::: at all"},
        secrets=Secrets(),
    )
    # ollama_is_local("not a valid url :::: at all") is True (no "ollama.com"
    # substring), so no key is required here — the only thing standing
    # between this config and a raised ValueError is the profile_text=None
    # check running before build_binding does.
    assert _build_relevance_scorer(cfg, None) is None


def test_tailor_engine_refuses_unsupported_provider_before_checking_its_key(caplog):
    """Regression: a non-Ollama provider must be refused for being
    unsupported (tailoring_unsupported_provider), not for lacking a key it
    could never use anyway (tailoring_disabled_at_runtime) — advising the
    user to add a key that cannot help is actively misleading."""
    from src.tailor import build_tailor_engine
    from src.tailor.models import EvidenceBank, ResumeContent

    cfg = AppConfig(
        tailoring={"enabled": True, "provider": "anthropic"},
        secrets=Secrets(),  # no anthropic_api_key
    )
    with caplog.at_level("WARNING", logger="src.tailor"):
        result = build_tailor_engine(
            cfg,
            ResumeContent(name="A", contact={}, skills=[], experiences=[]),
            EvidenceBank(),
        )
    assert result is None
    events = [r.message for r in caplog.records]
    assert "tailoring_unsupported_provider" in events
    assert "tailoring_disabled_at_runtime" not in events
