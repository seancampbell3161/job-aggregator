"""Résumé tailoring (sub-project A): foundation artifacts + engine + CLI.

build_tailor_engine mirrors handler._build_relevance_scorer — provider/model
default to the relevance values, the Ollama host comes from
relevance.ollama_host, fail-soft to None when disabled / key missing /
documents missing (the caller treats None as 'tailoring unavailable')."""

from __future__ import annotations

import logging

from src.config import AppConfig
from src.llm.providers import build_binding, missing_key, resolve
from src.tailor.engine import OllamaTailorEngine
from src.tailor.models import EvidenceBank, ResumeContent

log = logging.getLogger(__name__)


def build_tailor_engine(
    cfg: AppConfig, content: ResumeContent | None, evidence: EvidenceBank | None,
) -> OllamaTailorEngine | None:
    t = cfg.tailoring
    if not t.enabled:
        return None

    provider, _ = resolve(cfg, "tailoring")
    if provider != "ollama":  # sub-project 6 wires the other providers
        log.warning("tailoring_unsupported_provider", extra={"provider": provider})
        return None

    key = missing_key(cfg, "tailoring")
    if key is not None:
        log.warning("tailoring_disabled_at_runtime", extra={"reason": f"{key} not set"})
        return None

    if content is None or evidence is None:
        log.warning("tailoring_disabled_at_runtime",
                    extra={"reason": "resume_content or evidence document missing"})
        return None

    binding = build_binding(cfg, feature="tailoring", timeout_seconds=t.timeout_seconds)
    if binding is None:
        # Defensive: the provider/key checks above already confirmed this
        # binding should succeed.
        log.warning("tailoring_disabled_at_runtime", extra={"reason": "no provider binding"})
        return None

    return OllamaTailorEngine(
        client=binding.client, model=binding.model, content=content, evidence=evidence,
        timeout_seconds=binding.timeout_seconds,
    )
