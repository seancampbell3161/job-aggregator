"""The properties the port must not break."""
import inspect

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
