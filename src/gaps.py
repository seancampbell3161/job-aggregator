"""LLM résumé gap analysis.

For each posting we're about to notify, compare the posting's required hard
skills against the user's résumé and return the gaps — concrete technologies the
role wants that the résumé does not evidence. This is a SEPARATE call from
relevance scoring and never influences the relevance score: it runs only on
postings that already cleared scoring/suppression, so recall is unchanged.

Three provider implementations mirror src/relevance.py and share its fail-open
contract: any failure (network, timeout, rate limit, malformed response) returns
Gaps(skills=[], is_fallback=True) — gaps are an annotation, so a failure must
never block a notification. An empty skill list with is_fallback=False is a
legitimate "clean match" result, not a failure."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Protocol

from src.models import NormalizedPosting
# Reuse the relevance helpers (DRY): posting → user message, and tolerant JSON
# object extraction from fenced/prose model output.
from src.relevance import _extract_json_object, _format_user_message

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Gaps:
    skills: list[str]      # canonical hard-skill names the posting requires, absent from the résumé
    is_fallback: bool      # True when the LLM call failed (skills is then always [])
    error_type: str | None = None  # exception class name, or "MalformedResponse"; None on success


def _fallback_gaps(error_type: str) -> Gaps:
    """The fail-open gap sentinel, tagged with what went wrong for /pipeline."""
    return Gaps(skills=[], is_fallback=True, error_type=error_type)


class GapAnalyzer(Protocol):
    async def analyze(self, posting: NormalizedPosting) -> Gaps: ...


_GAP_INSTRUCTIONS = (
    "You compare a job posting against a candidate's résumé and identify skill gaps.\n"
    "List ONLY the hard technical skills the posting REQUIRES that the résumé does not\n"
    "evidence. Hard skills are concrete technologies, languages, frameworks, tools, or\n"
    "platforms (e.g. Kubernetes, Kafka, Terraform, Go) — never soft skills, seniority,\n"
    "or years of experience. Use canonical names: 'Kubernetes' not 'k8s', 'Go' not\n"
    "'Golang', 'PostgreSQL' not 'postgres'. If the résumé already covers everything the\n"
    "posting requires, return an empty list. Return at most {max} skills."
)

_GAP_OUTPUT_HINT = (
    "\nRespond with ONLY a JSON object and nothing else, in exactly this form:\n"
    '{"missing_skills": ["<skill>", ...]}'
)


def _gap_tool(max_skills: int) -> dict:
    return {
        "name": "record_gaps",
        "description": "Record the hard skills the posting requires that the résumé lacks.",
        "input_schema": {
            "type": "object",
            "properties": {
                "missing_skills": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": max_skills,
                },
            },
            "required": ["missing_skills"],
        },
    }


def _gap_response_schema(max_skills: int) -> dict:
    return {
        "type": "object",
        "properties": {
            "missing_skills": {"type": "array", "items": {"type": "string"}, "maxItems": max_skills},
        },
        "required": ["missing_skills"],
    }


def _system_text(resume_md: str, max_skills: int) -> str:
    return (
        _GAP_INSTRUCTIONS.format(max=max_skills)
        + "\n----- CANDIDATE RÉSUMÉ -----\n"
        + resume_md
        + "\n----- END RÉSUMÉ -----\n"
    )


def _skills_from_list(raw: Any, max_skills: int) -> list[str]:
    """Coerce a raw JSON value into a clean, bounded list of skill strings."""
    if not isinstance(raw, list):
        raise ValueError("missing_skills is not a list")
    cleaned = [str(s).strip() for s in raw if str(s).strip()]
    return cleaned[:max_skills]


class AnthropicGapAnalyzer:
    """Anthropic implementation. Résumé sits in the cache_control: ephemeral
    system prefix so prompt caching amortizes it across survivors in a cycle."""

    def __init__(self, *, client: Any, model: str, resume_md: str, timeout_seconds: int, max_skills: int) -> None:
        self._client = client
        self._model = model
        self._resume = resume_md
        self._timeout = timeout_seconds
        self._max_skills = max_skills

    async def analyze(self, posting: NormalizedPosting) -> Gaps:
        try:
            resp = await asyncio.wait_for(
                self._client.messages.create(
                    model=self._model,
                    max_tokens=300,
                    timeout=self._timeout,
                    system=[
                        {
                            "type": "text",
                            "text": _system_text(self._resume, self._max_skills),
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                    messages=[{"role": "user", "content": _format_user_message(posting)}],
                    tools=[_gap_tool(self._max_skills)],
                    tool_choice={"type": "tool", "name": "record_gaps"},
                ),
                # The SDK timeout is per attempt and it retries; this bounds the whole call.
                timeout=self._timeout,
            )
        except Exception as exc:  # noqa: BLE001 — fail-open is the contract
            log.warning(
                "gap_llm_call_failed",
                extra={"job_id": posting.job_id, "provider": "anthropic", "error": str(exc), "error_type": type(exc).__name__},
            )
            return _fallback_gaps(type(exc).__name__)

        for block in getattr(resp, "content", []):
            if getattr(block, "type", None) != "tool_use":
                continue
            if getattr(block, "name", None) != "record_gaps":
                continue
            inp = getattr(block, "input", None) or {}
            if "missing_skills" not in inp:
                break
            try:
                skills = _skills_from_list(inp["missing_skills"], self._max_skills)
            except ValueError:
                break
            return Gaps(skills=skills, is_fallback=False)

        log.warning("gap_llm_malformed_response", extra={"job_id": posting.job_id, "provider": "anthropic"})
        return _fallback_gaps("MalformedResponse")


class GeminiGapAnalyzer:
    """Google Gen AI implementation. JSON-schema structured output; per-call
    timeout via asyncio.wait_for (SDK timeout is client-level)."""

    def __init__(self, *, client: Any, model: str, resume_md: str, timeout_seconds: int, max_skills: int) -> None:
        self._client = client
        self._model = model
        self._resume = resume_md
        self._timeout = timeout_seconds
        self._max_skills = max_skills

    def _build_config(self) -> Any:
        system_text = _system_text(self._resume, self._max_skills)
        schema = _gap_response_schema(self._max_skills)
        try:
            from google.genai import types  # type: ignore
            return types.GenerateContentConfig(
                system_instruction=system_text,
                response_mime_type="application/json",
                response_json_schema=schema,
                max_output_tokens=300,
            )
        except ImportError:
            return {
                "system_instruction": system_text,
                "response_mime_type": "application/json",
                "response_json_schema": schema,
                "max_output_tokens": 300,
            }

    async def analyze(self, posting: NormalizedPosting) -> Gaps:
        try:
            resp = await asyncio.wait_for(
                self._client.aio.models.generate_content(
                    model=self._model,
                    contents=_format_user_message(posting),
                    config=self._build_config(),
                ),
                timeout=self._timeout,
            )
        except Exception as exc:  # noqa: BLE001 — fail-open is the contract
            log.warning(
                "gap_llm_call_failed",
                extra={"job_id": posting.job_id, "provider": "gemini", "error": str(exc), "error_type": type(exc).__name__},
            )
            return _fallback_gaps(type(exc).__name__)

        raw_text = getattr(resp, "text", None)
        return _parse_missing_skills(raw_text, job_id=posting.job_id, provider="gemini", max_skills=self._max_skills)


_OLLAMA_NUM_PREDICT = 1024  # headroom: reasoning models burn budget before the JSON answer


def _ollama_response_text(resp: Any) -> str | None:
    msg = getattr(resp, "message", None)
    if msg is None and isinstance(resp, dict):
        msg = resp.get("message")
    if msg is None:
        return None
    content = getattr(msg, "content", None)
    if content is None and isinstance(msg, dict):
        content = msg.get("content")
    return content


class OllamaGapAnalyzer:
    """Ollama Cloud implementation. No schema enforcement on Cloud, so output is
    prompt-engineered JSON parsed defensively via the shared parser."""

    def __init__(self, *, client: Any, model: str, resume_md: str, timeout_seconds: int, max_skills: int) -> None:
        self._client = client
        self._model = model
        self._resume = resume_md
        self._timeout = timeout_seconds
        self._max_skills = max_skills

    def _system_msg(self) -> str:
        return _system_text(self._resume, self._max_skills) + _GAP_OUTPUT_HINT

    async def analyze(self, posting: NormalizedPosting) -> Gaps:
        try:
            resp = await asyncio.wait_for(
                self._client.chat(
                    model=self._model,
                    messages=[
                        {"role": "system", "content": self._system_msg()},
                        {"role": "user", "content": _format_user_message(posting)},
                    ],
                    think=False,
                    options={"temperature": 0, "num_predict": _OLLAMA_NUM_PREDICT},
                ),
                timeout=self._timeout,
            )
        except Exception as exc:  # noqa: BLE001 — fail-open is the contract
            log.warning(
                "gap_llm_call_failed",
                extra={"job_id": posting.job_id, "provider": "ollama", "error": str(exc), "error_type": type(exc).__name__},
            )
            return _fallback_gaps(type(exc).__name__)

        content = _ollama_response_text(resp)
        if not isinstance(content, str):
            content = None
        return _parse_missing_skills(content, job_id=posting.job_id, provider="ollama", max_skills=self._max_skills)


def _parse_missing_skills(raw_text: str | None, *, job_id: str, provider: str, max_skills: int) -> Gaps:
    """Parse a {"missing_skills": [...]} object out of free-form model text.
    Any failure logs gap_llm_malformed_response and returns the fail-open
    sentinel. Shared by the Gemini and Ollama analyzers."""
    if not raw_text:
        log.warning("gap_llm_malformed_response", extra={"job_id": job_id, "provider": provider, "reason": "empty text"})
        return _fallback_gaps("MalformedResponse")
    obj = _extract_json_object(raw_text)
    if obj is None or "missing_skills" not in obj:
        log.warning("gap_llm_malformed_response", extra={"job_id": job_id, "provider": provider, "reason": "no missing_skills"})
        return _fallback_gaps("MalformedResponse")
    try:
        skills = _skills_from_list(obj["missing_skills"], max_skills)
    except ValueError:
        log.warning("gap_llm_malformed_response", extra={"job_id": job_id, "provider": provider, "reason": "not a list"})
        return _fallback_gaps("MalformedResponse")
    return Gaps(skills=skills, is_fallback=False)
