import json

from src.config import AppConfig, RelevanceConfig, Secrets, TailoringConfig
from src.tailor import build_tailor_engine
from src.tailor.engine import OllamaTailorEngine


def _cfg(tmp_path, *, enabled=True, key="k", content=True, evidence=True):
    cdir = tmp_path
    if content:
        (cdir / "content.json").write_text((open("resume/content.example.json").read()))
    if evidence:
        (cdir / "evidence.json").write_text(json.dumps({"projects": []}))
    return AppConfig.model_construct(
        tailoring=TailoringConfig(enabled=enabled,
                                  content_path=str(cdir / "content.json"),
                                  evidence_path=str(cdir / "evidence.json")),
        relevance=RelevanceConfig(provider="ollama", model="gpt-oss:120b"),
        secrets=Secrets(ntfy_topic_url="x", discord_webhook_url="x", ollama_api_key=key),
    )


def test_factory_builds_ollama_engine(tmp_path):
    engine = build_tailor_engine(_cfg(tmp_path))
    assert isinstance(engine, OllamaTailorEngine)
    assert engine._model == "gpt-oss:120b"


def test_factory_none_when_disabled(tmp_path):
    assert build_tailor_engine(_cfg(tmp_path, enabled=False)) is None


def test_factory_none_when_key_missing(tmp_path):
    assert build_tailor_engine(_cfg(tmp_path, key="")) is None


def test_factory_none_when_artifacts_missing(tmp_path):
    assert build_tailor_engine(_cfg(tmp_path, content=False)) is None
