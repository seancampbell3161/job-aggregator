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
    assert build_tailor_engine(_cfg(), CONTENT, None) is None
