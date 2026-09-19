"""The one place that turns settings into an LLM client."""
import pytest

from src.config import AppConfig, Secrets
from src.llm.providers import (
    PROVIDER_KEYS, LlmBinding, build_binding, needs_key, resolve,
)


def _cfg(**kw) -> AppConfig:
    return AppConfig(**kw)


def test_provider_keys_cover_every_relevance_provider():
    """A provider added to the Literal without a key entry here would make
    build_binding silently return None for every feature."""
    literal = AppConfig.model_fields["relevance"].annotation.model_fields["provider"].annotation
    from typing import get_args
    assert set(get_args(literal)) == set(PROVIDER_KEYS)


def test_resolve_falls_back_to_relevance():
    cfg = _cfg(relevance={"provider": "gemini", "model": "gemini-2.0"})
    assert resolve(cfg, "gap_analysis") == ("gemini", "gemini-2.0")


def test_resolve_prefers_the_feature_override():
    cfg = _cfg(
        relevance={"provider": "gemini", "model": "gemini-2.0"},
        gap_analysis={"provider": "anthropic", "model": "claude-haiku-4-5"},
    )
    assert resolve(cfg, "gap_analysis") == ("anthropic", "claude-haiku-4-5")


def test_resolve_on_relevance_itself():
    cfg = _cfg(relevance={"provider": "ollama", "model": "gpt-oss"})
    assert resolve(cfg, "relevance") == ("ollama", "gpt-oss")


def test_local_ollama_needs_no_key():
    cfg = _cfg(relevance={"ollama_host": "http://ollama:11434"})
    assert needs_key(cfg, "ollama") is False
    assert needs_key(cfg, "anthropic") is True


def test_cloud_ollama_needs_a_key():
    cfg = _cfg(relevance={"ollama_host": "https://ollama.com"})
    assert needs_key(cfg, "ollama") is True


def test_binding_is_none_without_the_key():
    cfg = _cfg(relevance={"provider": "anthropic"})
    assert build_binding(cfg, feature="relevance", timeout_seconds=10) is None


def test_binding_builds_for_local_ollama_without_a_key():
    cfg = _cfg(relevance={"provider": "ollama", "model": "m", "ollama_host": "http://ollama:11434"})
    binding = build_binding(cfg, feature="relevance", timeout_seconds=7)
    assert isinstance(binding, LlmBinding)
    assert (binding.provider, binding.model, binding.timeout_seconds) == ("ollama", "m", 7)
    assert binding.client is not None


def test_binding_passes_the_ollama_host_and_auth_header():
    cfg = _cfg(
        relevance={"provider": "ollama", "model": "m", "ollama_host": "https://ollama.com"},
        secrets=Secrets(ollama_api_key="k"),
    )
    binding = build_binding(cfg, feature="relevance", timeout_seconds=5)
    assert "ollama.com" in str(binding.client._client.base_url)
    assert binding.client._client.headers["authorization"] == "Bearer k"


def test_unknown_provider_returns_none(monkeypatch):
    """Defence in depth against a Literal that grows without a key entry —
    readiness.py takes the same posture rather than guessing a secret name."""
    cfg = _cfg(relevance={"provider": "anthropic"}, secrets=Secrets(anthropic_api_key="k"))
    monkeypatch.delitem(PROVIDER_KEYS, "anthropic")
    assert build_binding(cfg, feature="relevance", timeout_seconds=1) is None
