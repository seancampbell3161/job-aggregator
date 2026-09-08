from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Protocol, Sequence

import httpx


@dataclass(frozen=True)
class NotificationPayload:
    title: str               # "Senior Backend Engineer @ Stripe" (NO score prefix here; sinks add it)
    company: str
    role: str                # original posting title
    location: str            # "Remote (US)" or "[loc?] San Francisco, CA"
    comp: str | None         # "$180k-$240k" or None
    stack_matched: list[str]
    posted: str              # "2 min ago" or "unknown"
    apply_url: str
    source: str
    seniority: str | None    # for color coding
    tags: list[str] = field(default_factory=list)  # ntfy tags
    # Plan 4: relevance scoring
    relevance_score: int | None = None              # 0..10, or None if disabled / fallback
    relevance_rationale: str | None = None          # "Strong fit" or "(LLM unavailable)"
    relevance_priority: str = "default"             # "max" | "default" | "min" — sinks translate
    gaps: list[str] = field(default_factory=list)   # résumé skill gaps; [] when feature off / clean match
    tailor_url: str | None = None   # signed hosted-tailor deep-link, or None when the endpoint isn't configured


class Sink(Protocol):
    name: str

    async def send(self, client: httpx.AsyncClient, payload: NotificationPayload) -> None: ...


async def fanout(
    client: httpx.AsyncClient,
    payload: NotificationPayload,
    sinks: Sequence["Sink"],
) -> dict[str, bool | Exception]:
    """Send to all sinks in parallel. Returns {sink_name: True | Exception}."""
    results = await asyncio.gather(
        *(sink.send(client, payload) for sink in sinks),
        return_exceptions=True,
    )
    return {
        sink.name: (True if not isinstance(res, BaseException) else res)
        for sink, res in zip(sinks, results)
    }
