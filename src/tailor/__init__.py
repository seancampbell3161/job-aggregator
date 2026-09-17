"""Résumé tailoring (sub-project A): foundation artifacts + engine + CLI.

build_tailor_engine mirrors handler._build_relevance_scorer — provider/model
default to the relevance values, the Ollama host comes from
relevance.ollama_host, fail-soft to None when disabled / key missing /
documents missing (the caller treats None as 'tailoring unavailable')."""

from __future__ import annotations

import logging

from src.config import AppConfig
from src.tailor.engine import OllamaTailorEngine
from src.tailor.models import EvidenceBank, ResumeContent

log = logging.getLogger(__name__)


def build_tailor_engine(
    cfg: AppConfig, content: ResumeContent | None, evidence: EvidenceBank | None,
) -> OllamaTailorEngine | None:
    t = cfg.tailoring
    if not t.enabled:
        return None

    provider = t.provider or cfg.relevance.provider
    model = t.model or cfg.relevance.model
    if provider != "ollama":  # sub-project A wires only the deployed provider
        log.warning("tailoring_unsupported_provider", extra={"provider": provider})
        return None

    api_key = cfg.secrets.ollama_api_key
    if not cfg.relevance.ollama_is_local and not api_key:
        log.warning("tailoring_disabled_at_runtime", extra={"reason": "ollama_api_key not set"})
        return None

    if content is None or evidence is None:
        log.warning("tailoring_disabled_at_runtime",
                    extra={"reason": "resume_content or evidence document missing"})
        return None

    from ollama import AsyncClient
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    client = AsyncClient(host=cfg.relevance.ollama_host, headers=headers)
    return OllamaTailorEngine(
        client=client, model=model, content=content, evidence=evidence,
        timeout_seconds=t.timeout_seconds,
    )
