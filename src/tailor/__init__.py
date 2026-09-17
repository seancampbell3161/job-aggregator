"""Résumé tailoring (sub-project A): foundation artifacts + engine + CLI.

build_tailor_engine mirrors handler._build_relevance_scorer — provider/model
default to the relevance values, ollama key from env, fail-soft to None when
disabled / key missing / artifacts unreadable (the caller treats None as
'tailoring unavailable')."""

from __future__ import annotations

import logging

from src.config import AppConfig
from src.tailor.content import load_content
from src.tailor.engine import OllamaTailorEngine
from src.tailor.evidence import load_evidence

log = logging.getLogger(__name__)

_OLLAMA_HOST = "https://ollama.com"


def build_tailor_engine(cfg: AppConfig) -> OllamaTailorEngine | None:
    t = cfg.tailoring
    if not t.enabled:
        return None

    provider = t.provider or cfg.relevance.provider
    model = t.model or cfg.relevance.model
    if provider != "ollama":  # sub-project A wires only the deployed provider
        log.warning("tailoring_unsupported_provider", extra={"provider": provider})
        return None

    api_key = cfg.secrets.ollama_api_key
    if not api_key:
        log.warning("tailoring_disabled_at_runtime", extra={"reason": "ollama_api_key not set"})
        return None

    try:
        content = load_content(t.content_path)
        evidence = load_evidence(t.evidence_path)
    except (OSError, ValueError) as exc:
        log.warning("tailoring_disabled_at_runtime",
                    extra={"reason": "artifacts unreadable", "error": str(exc)})
        return None

    from ollama import AsyncClient
    client = AsyncClient(host=_OLLAMA_HOST, headers={"Authorization": f"Bearer {api_key}"})
    return OllamaTailorEngine(
        client=client, model=model, content=content, evidence=evidence,
        timeout_seconds=t.timeout_seconds,
    )
