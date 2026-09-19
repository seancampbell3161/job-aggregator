import json

from src.config import AppConfig, RelevanceConfig, Secrets, TailoringConfig
from src.tailor import build_tailor_engine
from src.tailor.content import load_content
from src.tailor.engine import TailorEngine
from src.tailor.evidence import parse_evidence

CONTENT = load_content("resume/content.example.json")
EVIDENCE = parse_evidence(json.dumps({"projects": []}))


def _cfg(*, enabled=True, key="k", host="https://ollama.com"):
    return AppConfig(
        tailoring=TailoringConfig(enabled=enabled),
        relevance=RelevanceConfig(provider="ollama", model="gpt-oss:120b", ollama_host=host),
        secrets=Secrets(ollama_api_key=key),
    )


def test_factory_builds_ollama_engine():
    engine = build_tailor_engine(_cfg(), CONTENT, EVIDENCE)
    assert isinstance(engine, TailorEngine)
    assert engine._binding.model == "gpt-oss:120b"


def test_factory_none_when_disabled():
    assert build_tailor_engine(_cfg(enabled=False), CONTENT, EVIDENCE) is None


def test_factory_none_when_cloud_key_missing():
    assert build_tailor_engine(_cfg(key=""), CONTENT, EVIDENCE) is None


def test_factory_local_host_needs_no_key(monkeypatch):
    captured = {}

    class FakeClient:
        def __init__(self, host=None, headers=None):
            captured.update(host=host, headers=headers)

    monkeypatch.setattr("ollama.AsyncClient", FakeClient)
    assert build_tailor_engine(_cfg(key="", host="http://ollama:11434"), CONTENT, EVIDENCE) is not None
    assert captured == {"host": "http://ollama:11434", "headers": {}}


def test_factory_none_when_documents_missing():
    assert build_tailor_engine(_cfg(), None, EVIDENCE) is None


def _cfg_anthropic():
    return AppConfig(
        tailoring=TailoringConfig(enabled=True, provider="anthropic"),
        relevance=RelevanceConfig(provider="ollama", model="gpt-oss:120b",
                                  ollama_host="https://ollama.com"),
        secrets=Secrets(anthropic_api_key="sk-test"),
    )


def test_factory_builds_an_anthropic_engine():
    """The guard this sub-project exists to remove: a Claude user had scoring,
    gaps, coaching and drafting working and tailoring silently unavailable."""
    engine = build_tailor_engine(_cfg_anthropic(), CONTENT, EVIDENCE)
    assert engine is not None
    assert engine._binding.provider == "anthropic"


def test_factory_builds_a_gemini_engine():
    cfg = AppConfig(
        tailoring=TailoringConfig(enabled=True, provider="gemini"),
        relevance=RelevanceConfig(provider="ollama", model="gpt-oss:120b",
                                  ollama_host="https://ollama.com"),
        secrets=Secrets(google_api_key="g-test"),
    )
    engine = build_tailor_engine(cfg, CONTENT, EVIDENCE)
    assert engine is not None
    assert engine._binding.provider == "gemini"


def test_factory_still_refuses_a_provider_with_no_key():
    cfg = AppConfig(
        tailoring=TailoringConfig(enabled=True, provider="anthropic"),
        relevance=RelevanceConfig(provider="ollama", model="m",
                                  ollama_host="https://ollama.com"),
        secrets=Secrets(),
    )
    assert build_tailor_engine(cfg, CONTENT, EVIDENCE) is None


def test_factory_builds_without_any_evidence_document():
    """evidence.json is digested from a Jira CSV export. Requiring it made
    tailoring unreachable for everyone who has no such export."""
    engine = build_tailor_engine(_cfg(), CONTENT, None)
    assert engine is not None
    assert engine._evidence.projects == []


def test_factory_still_refuses_without_content():
    """Unlike evidence, content is genuinely required — there is nothing to
    rewrite without it."""
    assert build_tailor_engine(_cfg(), None, EVIDENCE) is None
