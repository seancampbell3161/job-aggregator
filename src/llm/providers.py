"""Settings -> an LLM client, in one place.

Before this module, five factories (relevance, gap analysis, coach, tailoring,
the docx template importer) each re-derived the same four things by hand: the
provider/model fallback to the relevance settings, the provider -> secret
mapping, the rule that only a *local* Ollama needs no API key, and the client
construction itself. readiness.py held a fifth copy of the mapping. A provider
added to the Literal in src/config.py had to be remembered in six places.

This module owns all four. What it deliberately does NOT own is how each
feature prompts, what schema it asks for, how it parses the answer, or how it
retries — those differ per feature on purpose (only relevance retries Ollama,
and each feature has its own timeout)."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from src.config import AppConfig

log = logging.getLogger(__name__)

# provider -> the Secrets field holding its API key. The single source of
# truth: src/web/settings/readiness.py imports this rather than restating it.
PROVIDER_KEYS: dict[str, str] = {
    "anthropic": "anthropic_api_key",
    "gemini": "google_api_key",
    "ollama": "ollama_api_key",
}


@dataclass(frozen=True)
class LlmBinding:
    """A ready-to-call provider client plus the two things every call site
    needs alongside it. `client` is the provider's own SDK object, so callers
    still speak the provider's dialect — this is a wiring seam, not a
    pretend-every-provider-is-the-same abstraction."""
    provider: str
    model: str
    client: Any
    timeout_seconds: int


def resolve(cfg: AppConfig, feature: str) -> tuple[str, str]:
    """(provider, model) for a feature section, falling back to relevance.

    Works for `relevance` itself, whose fields are non-optional: the `or`
    simply returns the same value."""
    section = getattr(cfg, feature)
    provider = getattr(section, "provider", None) or cfg.relevance.provider
    model = getattr(section, "model", None) or cfg.relevance.model
    return provider, model


def needs_key(cfg: AppConfig, provider: str) -> bool:
    """Only a *local* Ollama runs without an API key. ollama_is_local is a
    property of the relevance section because ollama_host lives there and is
    shared by every Ollama-backed feature."""
    return not (provider == "ollama" and cfg.relevance.ollama_is_local)


def build_client(cfg: AppConfig, provider: str, api_key: str) -> Any:
    """The provider's own async SDK client. Imports are local so an install
    that never uses gemini never imports google-genai."""
    if provider == "anthropic":
        from anthropic import AsyncAnthropic
        return AsyncAnthropic(api_key=api_key)
    if provider == "ollama":
        from ollama import AsyncClient
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        return AsyncClient(host=cfg.relevance.ollama_host, headers=headers)
    from google import genai  # type: ignore
    return genai.Client(api_key=api_key)


def build_binding(
    cfg: AppConfig, *, feature: str, timeout_seconds: int
) -> LlmBinding | None:
    """None when the feature cannot run: unknown provider, or a required key
    that is not set. Callers treat None as "feature inert" — the same contract
    the five factories already had.

    Note this does NOT check the feature's `enabled` flag or its required
    documents: those are the caller's business, and they differ (relevance
    needs a profile, gap analysis a résumé, coach neither)."""
    provider, model = resolve(cfg, feature)
    key_name = PROVIDER_KEYS.get(provider)
    if key_name is None:
        # A provider in the Literal with no key entry here. Refuse rather than
        # guess which secret it would need.
        log.warning("llm_binding_unknown_provider",
                    extra={"feature": feature, "provider": provider})
        return None
    api_key = getattr(cfg.secrets, key_name)
    if needs_key(cfg, provider) and not api_key:
        log.warning("llm_binding_unavailable",
                    extra={"feature": feature, "provider": provider,
                           "reason": f"{key_name} not set"})
        return None
    return LlmBinding(
        provider=provider, model=model,
        client=build_client(cfg, provider, api_key),
        timeout_seconds=timeout_seconds,
    )
