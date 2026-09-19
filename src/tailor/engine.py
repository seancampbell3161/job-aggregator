"""Tailoring engine: provider-agnostic LLM call + grounding-guarded result parser.

Calls the shared LLM seam (src.llm.structured.complete_json) rather than
Ollama directly, so tailoring works on any configured provider. The parser
enforces the no-fabrication policy DETERMINISTICALLY, regardless of model
compliance: parsing is content-driven — entries come from content.json in
content order; bullets without a real source_bullet_id for THEIR entry are
dropped (fabricated, duplicated, or emitted under the wrong entry),
evidence_refs are filtered to ticket ids that exist in the bank, and any
content bullet or entry the model omitted is restored in its original
wording. A non-fallback result therefore covers every content bullet exactly
once per entry. Same fail-open contract as relevance/gaps — tailor() never
raises."""

from __future__ import annotations

import logging

from src.llm.providers import LlmBinding
from src.llm.structured import complete_json
from src.sanitize import wrap_untrusted
from src.tailor.prompts import build_system_text
from src.tailor.models import (
    Bullet, EvidenceBank, FitAnalysis, ResumeContent,
    TailoredBullet, TailoredExperience, TailoredProject, TailorResult, SUMMARY_PLACEHOLDER,
)

log = logging.getLogger(__name__)


def _grounded_bullets(items, *, valid_ids: set[str], valid_refs: set[str],
                      job_id: str, entry_id: str) -> list[TailoredBullet]:
    """Drop bullets whose source_bullet_id isn't one of THIS entry's ids
    (fabricated, or emitted under the wrong entry) and duplicates (first
    occurrence wins); filter evidence_refs to real ticket ids. Skips non-dict
    items. Deterministic — independent of the model."""
    out: list[TailoredBullet] = []
    seen: set[str] = set()
    for b in items or []:
        if not isinstance(b, dict):
            continue
        sid = (b.get("source_bullet_id") or "").strip()
        if sid not in valid_ids:
            log.warning("tailor_fabricated_bullet_dropped",
                        extra={"job_id": job_id, "entry_id": entry_id, "source_bullet_id": sid})
            continue
        if sid in seen:
            log.warning("tailor_duplicate_bullet_dropped",
                        extra={"job_id": job_id, "entry_id": entry_id, "source_bullet_id": sid})
            continue
        seen.add(sid)
        refs = [r for r in (b.get("evidence_refs") or []) if r in valid_refs]
        out.append(TailoredBullet(source_bullet_id=sid, text=b.get("text", ""), evidence_refs=refs))
    return out


def _restore_omitted(bullets: list[TailoredBullet], content_bullets: list[Bullet], *,
                     valid_refs: set[str], job_id: str, entry_id: str) -> list[TailoredBullet]:
    """Completeness backstop: append, at the entry's tail, any content bullet the
    model omitted — original wording, original refs filtered to the bank. The
    model is never trusted for completeness, same as it is never trusted for
    grounding."""
    covered = {b.source_bullet_id for b in bullets}
    restored = list(bullets)
    for cb in content_bullets:
        if cb.id in covered:
            continue
        log.warning("tailor_omitted_bullet_restored",
                    extra={"job_id": job_id, "entry_id": entry_id, "source_bullet_id": cb.id})
        restored.append(TailoredBullet(
            source_bullet_id=cb.id, text=cb.text,
            evidence_refs=[r for r in cb.evidence_refs if r in valid_refs]))
    return restored


_BULLET_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "source_bullet_id": {"type": "string"},
        "text": {"type": "string"},
        "evidence_refs": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["source_bullet_id", "text"],
}

TAILOR_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "fit": {
            "type": "object",
            "properties": {
                "matches": {"type": "array", "items": {"type": "string"}},
                "gaps": {"type": "array", "items": {"type": "string"}},
                "overall": {"type": "string"},
            },
        },
        "experiences": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "experience_id": {"type": "string"},
                    "bullets": {"type": "array", "items": _BULLET_SCHEMA},
                },
                "required": ["experience_id", "bullets"],
            },
        },
        "projects": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string"},
                    "bullets": {"type": "array", "items": _BULLET_SCHEMA},
                },
                "required": ["project_id", "bullets"],
            },
        },
        "skills_ordered": {"type": "array", "items": {"type": "string"}},
        "summary_placeholder": {"type": "string"},
        "cover_letter": {"type": "string"},
    },
    "required": ["fit", "experiences", "skills_ordered", "cover_letter"],
}


def parse_tailor_result(
    obj: dict | None, *, content: ResumeContent, evidence: EvidenceBank, job_id: str,
) -> TailorResult:
    """Grounding-guarded result from the provider's parsed object.

    Takes the object rather than raw text: src.llm.structured.complete_json
    already does the tolerant extraction (plain JSON, fenced JSON, JSON in
    prose) this function used to open with, and on Anthropic the forced tool
    returns a dict with no text stage at all. None means the provider answered
    with nothing usable."""
    if obj is None:
        log.warning("tailor_malformed_response", extra={"job_id": job_id, "reason": "no json object"})
        return TailorResult.fallback()

    try:
        valid_refs = evidence.ticket_refs()

        model_exps: dict[str, dict] = {}
        for e in obj.get("experiences", []) or []:
            if isinstance(e, dict):
                model_exps.setdefault(e.get("experience_id", ""), e)
        experiences: list[TailoredExperience] = []
        for ce in content.experiences:
            raw = model_exps.get(ce.id)
            if raw is None:
                log.warning("tailor_omitted_entry_restored",
                            extra={"job_id": job_id, "entry_id": ce.id})
            grounded = _grounded_bullets((raw or {}).get("bullets"),
                                         valid_ids={b.id for b in ce.bullets},
                                         valid_refs=valid_refs, job_id=job_id, entry_id=ce.id)
            experiences.append(TailoredExperience(
                experience_id=ce.id,
                bullets=_restore_omitted(grounded, ce.bullets, valid_refs=valid_refs,
                                         job_id=job_id, entry_id=ce.id)))

        model_projs: dict[str, dict] = {}
        for p in obj.get("projects", []) or []:
            if isinstance(p, dict):
                model_projs.setdefault(p.get("project_id", ""), p)
        projects: list[TailoredProject] = []
        for cp in content.projects:
            raw = model_projs.get(cp.id)
            if raw is None:
                log.warning("tailor_omitted_entry_restored",
                            extra={"job_id": job_id, "entry_id": cp.id})
            grounded = _grounded_bullets((raw or {}).get("bullets"),
                                         valid_ids={b.id for b in cp.bullets},
                                         valid_refs=valid_refs, job_id=job_id, entry_id=cp.id)
            projects.append(TailoredProject(
                project_id=cp.id,
                bullets=_restore_omitted(grounded, cp.bullets, valid_refs=valid_refs,
                                         job_id=job_id, entry_id=cp.id)))

        valid_skill_names = {s.name.lower() for s in content.skills}
        skills_ordered = [s for s in (obj.get("skills_ordered", []) or [])
                          if isinstance(s, str) and s.lower() in valid_skill_names]

        fit_raw = obj.get("fit") or {}
        if not isinstance(fit_raw, dict):
            fit_raw = {}
        fit = FitAnalysis(
            matches=list(fit_raw.get("matches", []) or []),
            gaps=list(fit_raw.get("gaps", []) or []),
            overall=fit_raw.get("overall", "") or "",
        )
        return TailorResult(
            fit=fit,
            experiences=experiences,
            skills_ordered=skills_ordered,
            summary_placeholder=SUMMARY_PLACEHOLDER,  # always the marker — never model-authored prose
            cover_letter=obj.get("cover_letter", "") or "",
            projects=projects,
            is_fallback=False,
        )
    except Exception as exc:  # noqa: BLE001 — fail-open: malformed container types must not raise
        log.warning("tailor_malformed_response",
                    extra={"job_id": job_id, "reason": "unparseable structure",
                           "error": str(exc), "error_type": type(exc).__name__})
        return TailorResult.fallback()


_TAILOR_NUM_PREDICT = 16384  # rewrite-all returns EVERY bullet + cover letter + fit; gpt-oss also reasons first. 8192 was marginal — verbose runs peaked at ~7500 tok (91% of cap) and unlucky ones truncated mid-JSON -> tailor_malformed_response


class TailorEngine:
    """Provider-agnostic tailoring. Same fail-open contract as before: tailor()
    never raises, and a provider failure returns TailorResult.fallback() with
    is_fallback=True — the sentinel src/web/settings/probes.py:147-149 detects
    failure by. complete_json deliberately does NOT fail open, so absorbing
    that is this class's job."""

    def __init__(self, *, binding: LlmBinding, content: ResumeContent,
                 evidence: EvidenceBank) -> None:
        self._binding = binding
        self._content = content
        self._evidence = evidence
        self._system = build_system_text(content, evidence)

    async def tailor(self, *, job_id: str, jd_text: str) -> TailorResult:
        try:
            obj = await complete_json(
                self._binding,
                system=self._system,
                # Fenced, and the fence is breakout-proof — see
                # sanitize.wrap_untrusted. The policy prompt names this exact
                # tag as the untrusted region.
                user=wrap_untrusted(jd_text, "job_posting"),
                schema=TAILOR_SCHEMA,
                max_output_tokens=_TAILOR_NUM_PREDICT,
            )
        except Exception as exc:  # noqa: BLE001 — fail-open is the contract
            log.warning(
                "tailor_llm_call_failed",
                extra={"job_id": job_id, "provider": self._binding.provider,
                       "error": str(exc), "error_type": type(exc).__name__},
            )
            return TailorResult.fallback()

        return parse_tailor_result(obj, content=self._content,
                                   evidence=self._evidence, job_id=job_id)
