"""One JSON Schema in, one parsed dict out, whichever provider answers.

Every existing feature hand-writes its output schema twice — an Anthropic tool
schema and a Gemini response schema — and gives Ollama no schema at all, only
prompt instructions plus defensive parsing. This module does that translation
once.

It does NOT fail open. The four older features return `is_fallback=True`
sentinels on provider errors because a missed score must never stop a poll
cycle; résumé drafting has the opposite requirement — a silently empty profile
is indistinguishable from a successful draft — so provider exceptions
propagate and the caller decides. `None` means "the provider answered, but not
with usable JSON", which is a different thing from "the call failed"."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

# The tolerant parser the three scorers already share: plain JSON, ```json
# fences, or JSON embedded in prose.
from src.relevance import _extract_json_object
from src.llm.providers import LlmBinding

log = logging.getLogger(__name__)

_TOOL_NAME = "respond"

_OLLAMA_INSTRUCTIONS = (
    "Reply with a single JSON object and nothing else — no prose, no code "
    "fence, no explanation. It must match this JSON Schema exactly:\n{schema}"
)


async def complete_json(
    binding: LlmBinding, *, system: str, user: str, schema: dict
) -> dict | None:
    """The parsed object, or None if the provider produced nothing parseable."""
    if binding.provider == "anthropic":
        return await _anthropic(binding, system=system, user=user, schema=schema)
    if binding.provider == "gemini":
        return await _gemini(binding, system=system, user=user, schema=schema)
    return await _ollama(binding, system=system, user=user, schema=schema)


async def _anthropic(binding, *, system, user, schema) -> dict | None:
    resp = await binding.client.messages.create(
        model=binding.model,
        max_tokens=4096,
        system=system,
        tools=[{
            "name": _TOOL_NAME,
            "description": "Return the requested object.",
            "input_schema": schema,
        }],
        # Forcing the tool is what makes the schema binding rather than advisory.
        tool_choice={"type": "tool", "name": _TOOL_NAME},
        messages=[{"role": "user", "content": user}],
        timeout=binding.timeout_seconds,
    )
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use":
            return dict(block.input)
    # No tool block: usually max_tokens exhaustion mid-object.
    log.warning("complete_json_no_tool_block",
                extra={"stop_reason": getattr(resp, "stop_reason", None)})
    return None


async def _gemini(binding, *, system, user, schema) -> dict | None:
    config = _gemini_config(system, schema)
    resp = await asyncio.wait_for(
        binding.client.aio.models.generate_content(
            model=binding.model, contents=user, config=config,
        ),
        timeout=binding.timeout_seconds,
    )
    return _extract_json_object(getattr(resp, "text", "") or "")


def _gemini_config(system: str, schema: dict):
    """google.genai's typed config when the SDK exposes it, else the dict form
    the SDK also accepts — the same try/except the three existing Gemini
    call sites use."""
    values = {
        "system_instruction": system,
        "response_mime_type": "application/json",
        "response_json_schema": schema,
    }
    try:
        from google.genai import types  # type: ignore
        return types.GenerateContentConfig(**values)
    except Exception:  # noqa: BLE001 — SDK shape varies by version
        return values


async def _ollama(binding, *, system, user, schema) -> dict | None:
    """No schema support: instruct, then parse defensively."""
    system_text = system + "\n\n" + _OLLAMA_INSTRUCTIONS.format(
        schema=json.dumps(schema, indent=2)
    )
    resp = await asyncio.wait_for(
        binding.client.chat(
            model=binding.model,
            messages=[
                {"role": "system", "content": system_text},
                {"role": "user", "content": user},
            ],
        ),
        timeout=binding.timeout_seconds,
    )
    return _extract_json_object(_response_text(resp) or "")


def _response_text(resp: Any) -> str | None:
    """Ollama's SDK returns an object in some versions and a dict in others."""
    message = resp.get("message") if isinstance(resp, dict) else getattr(resp, "message", None)
    if message is None:
        return None
    return message.get("content") if isinstance(message, dict) else getattr(message, "content", None)
