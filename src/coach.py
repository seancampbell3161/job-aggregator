"""LLM response-rate coaching.

Build a curated JSON snapshot of the user's empirical job-search data (filter
config, profile, résumé bullet bank, application funnel, per-job outcomes,
rejection-audit aggregates) and ask an LLM for at most 7 structured
recommendation cards on improving application response rates.

All numbers are computed here, deterministically, BEFORE the LLM sees them —
the LLM analyzes, it does not calculate. Three provider implementations mirror
src/gaps.py and share its fail-open contract: any failure (network, timeout,
malformed response, zero valid cards) returns CoachResult(is_fallback=True)
and never raises."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

# Reuse the relevance helper (DRY): tolerant JSON object extraction from
# fenced/prose model output.
from src.gaps import _ollama_response_text
from src.relevance import _extract_json_object
from src.state import _KEPT_STATUSES
from src.tailor.models import ResumeContent
# funnel.py is pure geometry/reduction — no FastAPI import, safe from src/.
from src.web.funnel import APPLIED, _reduce, build_funnel, pipeline_rates

log = logging.getLogger(__name__)

_CATEGORIES = ("filters", "profile", "resume", "pipeline", "behavior")
_IMPACTS = ("high", "medium", "low")
_MAX_CARDS = 7
_LOW_SAMPLE_APPLICATIONS = 10
_TOP_N_AGGREGATES = 25  # cap Counter blocks so one noisy dimension can't flood the prompt


@dataclass(frozen=True)
class CoachCard:
    category: str   # one of _CATEGORIES
    title: str      # short imperative headline
    evidence: str   # the empirical claim, citing snapshot numbers
    action: str     # one concrete step (config key, bullet id, behavior)
    impact: str     # one of _IMPACTS


@dataclass(frozen=True)
class CoachResult:
    cards: list[CoachCard]
    is_fallback: bool               # True when the LLM call failed (cards is then always [])
    error_type: str | None = None   # exception class name, "MalformedResponse", or "EmptyRecommendations"


def _fallback_result(error_type: str) -> CoachResult:
    """The fail-open coach sentinel, tagged with what went wrong."""
    return CoachResult(cards=[], is_fallback=True, error_type=error_type)


class CoachEngine(Protocol):
    async def recommend(self, snapshot: "CoachSnapshot") -> CoachResult: ...


@dataclass(frozen=True)
class CoachSnapshot:
    """One serializable analysis input. Blocks are None when their source was
    unavailable (no content.json, no audit store, ...) — the prompt says so."""
    config: dict | None
    profile: str | None
    resume_bank: dict | None
    funnel: dict
    jobs: list[dict]
    aggregates: dict
    meta: dict

    def to_json(self) -> str:
        return json.dumps(asdict(self), default=str)


def _days_since(iso: str | None, now: datetime) -> int | None:
    try:
        return max(0, (now - datetime.fromisoformat(iso)).days)
    except (TypeError, ValueError):
        return None


def _job_row(m: dict, now: datetime) -> dict:
    """Compact per-job row. History entries can be malformed (see funnel._reduce's
    tolerance) — fall back to first_seen for the stage clock."""
    hist = [h for h in (m.get("history") or []) if isinstance(h, dict) and h.get("at")]
    last_at = hist[-1]["at"] if hist else m.get("first_seen")
    return {
        "title": m.get("title", ""),
        "company": m.get("company", ""),
        "source": (m.get("source") or "").split(":", 1)[0],
        "score": m.get("score"),
        "status": m.get("status", "new"),
        "gaps": list(m.get("gaps") or []),
        "days_since_first_seen": _days_since(m.get("first_seen"), now),
        "days_in_stage": _days_since(last_at, now),
    }


def _funnel_block(matches: list[dict]) -> dict:
    rates = pipeline_rates(matches)
    funnel = build_funnel(matches)
    return {
        "rates": [{"label": r.label, "num": r.num, "den": r.den, "pct": r.pct} for r in rates],
        "stage_counts": {n.id: n.count for n in funnel.nodes},
    }


def _resume_bank(content: ResumeContent) -> dict:
    def bullets(bs) -> list[dict]:
        return [{"id": b.id, "text": b.text, "tags": list(b.tags),
                 "metric_bearing": b.metric_bearing} for b in bs]
    return {
        "experiences": [
            {"id": e.id, "company": e.company, "role": e.role, "bullets": bullets(e.bullets)}
            for e in content.experiences
        ],
        "projects": [
            {"id": p.id, "name": p.name, "bullets": bullets(p.bullets)}
            for p in content.projects
        ],
        "skills": [s.name for s in content.skills],
    }


def _aggregates_block(matches: list[dict], pursued: list[dict], audit: dict | None) -> dict:
    applied_plus = [m for m in matches if _reduce(m)[0] >= APPLIED]
    gap_counter = Counter(g for m in applied_plus for g in (m.get("gaps") or []))
    out = {
        "gap_frequency_applied": dict(gap_counter.most_common(_TOP_N_AGGREGATES)),
        "pursued_by_company": dict(
            Counter(m.get("company", "") for m in pursued).most_common(_TOP_N_AGGREGATES)),
        "pursued_by_source": dict(
            Counter((m.get("source") or "").split(":", 1)[0] for m in pursued)
            .most_common(_TOP_N_AGGREGATES)),
    }
    if audit is not None:
        out["audit"] = audit
    return out


def build_snapshot(
    *,
    matches: list[dict],
    config: dict | None = None,
    profile_text: str | None = None,
    content: ResumeContent | None = None,
    audit: dict | None = None,
    max_jobs: int = 100,
    now: datetime | None = None,
) -> CoachSnapshot:
    """Deterministic snapshot assembly from injected inputs — no store or
    config access here, so it's trivially unit-testable. `config` and `audit`
    are pre-shaped dicts copied through verbatim."""
    now = now or datetime.now(timezone.utc)
    pursued = [m for m in matches if m.get("status") in _KEPT_STATUSES]
    pursued.sort(key=lambda m: m.get("first_seen") or "", reverse=True)
    applied_count = sum(1 for m in matches if _reduce(m)[0] >= APPLIED)
    return CoachSnapshot(
        config=config,
        profile=profile_text,
        resume_bank=_resume_bank(content) if content is not None else None,
        funnel=_funnel_block(matches),
        jobs=[_job_row(m, now) for m in pursued[:max_jobs]],
        aggregates=_aggregates_block(matches, pursued, audit),
        meta={
            "generated_at": now.isoformat(),
            "total_matches": len(matches),
            "applied_count": applied_count,
            "low_sample": applied_count < _LOW_SAMPLE_APPLICATIONS,
        },
    )


def _cards_from_list(raw: Any) -> list[CoachCard]:
    """Coerce the model's recommendations array into validated cards. Malformed
    entries (unknown category/impact, empty required fields, non-dicts) are
    dropped, not fatal; the list is capped at _MAX_CARDS."""
    if not isinstance(raw, list):
        raise ValueError("recommendations is not a list")
    cards: list[CoachCard] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        category = str(item.get("category", "")).strip().lower()
        impact = str(item.get("impact", "")).strip().lower()
        title = str(item.get("title", "")).strip()
        evidence = str(item.get("evidence", "")).strip()
        action = str(item.get("action", "")).strip()
        if category not in _CATEGORIES or impact not in _IMPACTS:
            continue
        if not (title and evidence and action):
            continue
        cards.append(CoachCard(category=category, title=title,
                               evidence=evidence, action=action, impact=impact))
    return cards[:_MAX_CARDS]


def _result_from_cards(cards: list[CoachCard], *, provider: str) -> CoachResult:
    """Zero surviving cards is an error run by contract — 'no advice' is not a
    useful result, so it must not render as a clean empty page."""
    if not cards:
        log.warning("coach_llm_empty_recommendations", extra={"provider": provider})
        return _fallback_result("EmptyRecommendations")
    return CoachResult(cards=cards, is_fallback=False)


def parse_coach_json(raw_text: str | None, *, provider: str) -> CoachResult:
    """Parse a {"recommendations": [...]} object out of free-form model text.
    Any failure logs coach_llm_malformed_response and returns the fail-open
    sentinel. Shared by the Gemini and Ollama engines."""
    if not raw_text:
        log.warning("coach_llm_malformed_response", extra={"provider": provider, "reason": "empty text"})
        return _fallback_result("MalformedResponse")
    obj = _extract_json_object(raw_text)
    if obj is None or "recommendations" not in obj:
        log.warning("coach_llm_malformed_response", extra={"provider": provider, "reason": "no recommendations"})
        return _fallback_result("MalformedResponse")
    try:
        cards = _cards_from_list(obj["recommendations"])
    except ValueError:
        log.warning("coach_llm_malformed_response", extra={"provider": provider, "reason": "not a list"})
        return _fallback_result("MalformedResponse")
    return _result_from_cards(cards, provider=provider)


_COACH_INSTRUCTIONS = (
    "You are a job-search analyst. You are given a JSON snapshot of one candidate's\n"
    "job-search system: filter configuration, profile, résumé bullet bank, application\n"
    "funnel, per-job outcomes, and rejection-audit aggregates. Recommend how to improve\n"
    "the odds of getting a RESPONSE to applications.\n"
    "Rules:\n"
    "- Return at most {max_cards} recommendations, ranked by expected impact, highest first.\n"
    "- Every `evidence` string MUST cite specific numbers or values present in the snapshot.\n"
    "- Every `action` MUST name the concrete thing to change: an exact config key with a\n"
    "  proposed value, a specific bullet id or profile passage to rewrite, or a specific\n"
    "  behavior tied to jobs in the data.\n"
    "- No generic career advice that does not follow from this snapshot.\n"
    "- If meta.low_sample is true, say so in the evidence of any card that leans on funnel\n"
    "  rates, and use impact medium or low for those cards.\n"
    "- category must be one of: filters, profile, resume, pipeline, behavior.\n"
    "- impact must be one of: high, medium, low."
)

_COACH_OUTPUT_HINT = (
    "\nRespond with ONLY a JSON object and nothing else, in exactly this form:\n"
    '{"recommendations": [{"category": "...", "title": "...", "evidence": "...", '
    '"action": "...", "impact": "..."}]}'
)

_ANTHROPIC_MAX_TOKENS = 2000
_OLLAMA_NUM_PREDICT = 4096  # reasoning models burn budget before the JSON answer


def _system_text() -> str:
    return _COACH_INSTRUCTIONS.format(max_cards=_MAX_CARDS)


def _user_message(snapshot: CoachSnapshot) -> str:
    notes = []
    if snapshot.config is None:
        notes.append("NOTE: filter configuration was not available for this run.")
    if snapshot.profile is None:
        notes.append("NOTE: no profile document was available for this run.")
    if snapshot.resume_bank is None:
        notes.append("NOTE: no résumé bullet bank was available for this run.")
    msg = "Snapshot of the candidate's job-search system:\n" + snapshot.to_json()
    return msg + ("\n" + "\n".join(notes) if notes else "")


def _card_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "category": {"type": "string", "enum": list(_CATEGORIES)},
            "title": {"type": "string"},
            "evidence": {"type": "string"},
            "action": {"type": "string"},
            "impact": {"type": "string", "enum": list(_IMPACTS)},
        },
        "required": ["category", "title", "evidence", "action", "impact"],
    }


def _coach_tool() -> dict:
    return {
        "name": "record_recommendations",
        "description": "Record the ranked recommendations for improving response rates.",
        "input_schema": {
            "type": "object",
            "properties": {
                "recommendations": {"type": "array", "items": _card_schema(), "maxItems": _MAX_CARDS},
            },
            "required": ["recommendations"],
        },
    }


def _coach_response_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "recommendations": {"type": "array", "items": _card_schema(), "maxItems": _MAX_CARDS},
        },
        "required": ["recommendations"],
    }


class AnthropicCoach:
    """Anthropic implementation: forced tool_use, so the output arrives as a
    validated-ish tool input rather than free text."""

    def __init__(self, *, client: Any, model: str, timeout_seconds: int) -> None:
        self._client = client
        self._model = model
        self._timeout = timeout_seconds

    async def recommend(self, snapshot: CoachSnapshot) -> CoachResult:
        try:
            resp = await asyncio.wait_for(
                self._client.messages.create(
                    model=self._model,
                    max_tokens=_ANTHROPIC_MAX_TOKENS,
                    timeout=self._timeout,
                    system=_system_text(),
                    messages=[{"role": "user", "content": _user_message(snapshot)}],
                    tools=[_coach_tool()],
                    tool_choice={"type": "tool", "name": "record_recommendations"},
                ),
                # The SDK timeout is per attempt and it retries; this bounds the whole call.
                timeout=self._timeout,
            )
        except Exception as exc:  # noqa: BLE001 — fail-open is the contract
            log.warning(
                "coach_llm_call_failed",
                extra={"provider": "anthropic", "error": str(exc), "error_type": type(exc).__name__},
            )
            return _fallback_result(type(exc).__name__)

        for block in getattr(resp, "content", []):
            if getattr(block, "type", None) != "tool_use":
                continue
            if getattr(block, "name", None) != "record_recommendations":
                continue
            inp = getattr(block, "input", None)
            if not isinstance(inp, dict) or "recommendations" not in inp:
                break
            try:
                cards = _cards_from_list(inp["recommendations"])
            except ValueError:
                break
            return _result_from_cards(cards, provider="anthropic")

        log.warning("coach_llm_malformed_response", extra={"provider": "anthropic"})
        return _fallback_result("MalformedResponse")


class GeminiCoach:
    """Google Gen AI implementation. JSON-schema structured output; per-call
    timeout via asyncio.wait_for (SDK timeout is client-level)."""

    def __init__(self, *, client: Any, model: str, timeout_seconds: int) -> None:
        self._client = client
        self._model = model
        self._timeout = timeout_seconds

    def _build_config(self) -> Any:
        try:
            from google.genai import types  # type: ignore
            return types.GenerateContentConfig(
                system_instruction=_system_text(),
                response_mime_type="application/json",
                response_json_schema=_coach_response_schema(),
                max_output_tokens=_ANTHROPIC_MAX_TOKENS,
            )
        except ImportError:
            return {
                "system_instruction": _system_text(),
                "response_mime_type": "application/json",
                "response_json_schema": _coach_response_schema(),
                "max_output_tokens": _ANTHROPIC_MAX_TOKENS,
            }

    async def recommend(self, snapshot: CoachSnapshot) -> CoachResult:
        try:
            resp = await asyncio.wait_for(
                self._client.aio.models.generate_content(
                    model=self._model,
                    contents=_user_message(snapshot),
                    config=self._build_config(),
                ),
                timeout=self._timeout,
            )
        except Exception as exc:  # noqa: BLE001 — fail-open is the contract
            log.warning(
                "coach_llm_call_failed",
                extra={"provider": "gemini", "error": str(exc), "error_type": type(exc).__name__},
            )
            return _fallback_result(type(exc).__name__)

        return parse_coach_json(getattr(resp, "text", None), provider="gemini")


class OllamaCoach:
    """Ollama implementation. No schema enforcement on Cloud, so output is
    prompt-engineered JSON parsed defensively via the shared parser."""

    def __init__(self, *, client: Any, model: str, timeout_seconds: int) -> None:
        self._client = client
        self._model = model
        self._timeout = timeout_seconds

    async def recommend(self, snapshot: CoachSnapshot) -> CoachResult:
        try:
            resp = await asyncio.wait_for(
                self._client.chat(
                    model=self._model,
                    messages=[
                        {"role": "system", "content": _system_text() + _COACH_OUTPUT_HINT},
                        {"role": "user", "content": _user_message(snapshot)},
                    ],
                    think=False,
                    options={"temperature": 0, "num_predict": _OLLAMA_NUM_PREDICT},
                ),
                timeout=self._timeout,
            )
        except Exception as exc:  # noqa: BLE001 — fail-open is the contract
            log.warning(
                "coach_llm_call_failed",
                extra={"provider": "ollama", "error": str(exc), "error_type": type(exc).__name__},
            )
            return _fallback_result(type(exc).__name__)

        content = _ollama_response_text(resp)
        if not isinstance(content, str):
            content = None
        return parse_coach_json(content, provider="ollama")
