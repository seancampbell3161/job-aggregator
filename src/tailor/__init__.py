"""Résumé tailoring (sub-project A): foundation artifacts + engine + CLI.

build_tailor_engine mirrors handler._build_relevance_scorer — provider/model
default to the relevance values, the Ollama host comes from
relevance.ollama_host, fail-soft to None when disabled / key missing / the
resume_content document missing (the caller treats None as 'tailoring
unavailable'). Evidence is optional: an absent bank degrades to an empty one
rather than refusing — most users have no Jira export to digest, and
src/tailor/prompts.py is evidence-aware so an empty bank cannot make the
model strip every metric it is shown."""

from __future__ import annotations

import logging

from src.config import AppConfig
from src.llm.providers import build_binding, missing_key
from src.tailor.engine import TailorEngine
from src.tailor.models import EvidenceBank, ResumeContent

log = logging.getLogger(__name__)


def build_tailor_engine(
    cfg: AppConfig, content: ResumeContent | None, evidence: EvidenceBank | None,
) -> TailorEngine | None:
    t = cfg.tailoring
    if not t.enabled:
        return None

    key = missing_key(cfg, "tailoring")
    if key is not None:
        log.warning("tailoring_disabled_at_runtime", extra={"reason": f"{key} not set"})
        return None

    # Content is required; evidence is not. An absent bank is the normal case
    # for anyone who has not digested a Jira export, and src/tailor/prompts.py
    # switches its citation rule to match so an empty bank cannot make the
    # model strip every metric it is shown.
    if content is None:
        log.warning("tailoring_disabled_at_runtime",
                    extra={"reason": "resume_content document missing"})
        return None

    binding = build_binding(cfg, feature="tailoring", timeout_seconds=t.timeout_seconds)
    if binding is None:
        # Defensive: the key check above already confirmed this should succeed.
        log.warning("tailoring_disabled_at_runtime", extra={"reason": "no provider binding"})
        return None

    return TailorEngine(
        binding=binding, content=content, evidence=evidence or EvidenceBank(),
    )
