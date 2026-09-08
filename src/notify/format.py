from __future__ import annotations

from datetime import datetime, timezone

from src.filters import Decision
from src.models import NormalizedPosting
from src.notify.base import NotificationPayload
from src.relevance import Score


def _humanize_age(posted_at: datetime | None) -> str:
    if posted_at is None:
        return "unknown"
    if posted_at.tzinfo is None:
        posted_at = posted_at.replace(tzinfo=timezone.utc)
    delta = datetime.now(tz=timezone.utc) - posted_at
    s = int(delta.total_seconds())
    if s < 60:
        return f"{s}s ago"
    m = s // 60
    if m < 60:
        return f"{m} min ago"
    h = m // 60
    if h < 24:
        return f"{h}h ago"
    d = h // 24
    return f"{d}d ago"


def _format_comp(lo: int | None, hi: int | None) -> str | None:
    if lo is None and hi is None:
        return None
    if lo and hi:
        return f"${lo // 1000}k-${hi // 1000}k"
    n = lo or hi
    return f"${n // 1000}k+"


def _format_location(text: str, tags: frozenset[str], unknown: bool) -> str:
    base = text or "unknown"
    if unknown:
        return f"[loc?] {base}"
    return base


def format_payload(
    p: NormalizedPosting,
    decision: Decision,
    *,
    stack_filter: list[str],
    score: "Score | None" = None,
    score_high: int = 7,
    score_low: int = 3,
    gaps: list[str] | None = None,
    tailor_url: str | None = None,
) -> NotificationPayload:
    matched_stack = sorted(p.stack & {s.lower() for s in stack_filter})
    location_unknown = "location" in decision.unknowns
    tags: list[str] = []
    if p.seniority:
        tags.append(p.seniority)
    for t in p.location_tags:
        if t in {"remote", "hybrid"}:
            tags.append(t)
    tags.extend(matched_stack)

    if score is None:
        rel_score: int | None = None
        rel_rationale: str | None = None
        rel_priority = "default"
    else:
        rel_score = score.value
        rel_rationale = score.rationale
        if score.value is None:
            rel_priority = "default"
        elif score.value >= score_high:
            rel_priority = "max"
        elif score.value <= score_low:
            rel_priority = "min"
        else:
            rel_priority = "default"

    return NotificationPayload(
        title=f"{p.title} @ {p.company}",
        company=p.company,
        role=p.title,
        location=_format_location(p.location_text, p.location_tags, location_unknown),
        comp=_format_comp(p.comp_min, p.comp_max),
        stack_matched=matched_stack,
        posted=_humanize_age(p.posted_at),
        apply_url=p.apply_url,
        source=p.source,
        seniority=p.seniority,
        tags=tags,
        relevance_score=rel_score,
        relevance_rationale=rel_rationale,
        relevance_priority=rel_priority,
        gaps=list(gaps or []),
        tailor_url=tailor_url,
    )
