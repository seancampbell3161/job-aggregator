from __future__ import annotations

from datetime import datetime, timezone

import httpx

from src.config import QuietHoursConfig
from src.notify.base import NotificationPayload


def _is_quiet(now_utc: datetime, qh: QuietHoursConfig) -> bool:
    local = now_utc.astimezone(qh.timezone).time()
    start, end = qh.start, qh.end
    if start <= end:
        return start <= local < end
    # wraparound (e.g. 23:00 → 07:00)
    return local >= start or local < end


class NtfySink:
    name = "ntfy"

    def __init__(self, *, topic_url: str, quiet_hours: QuietHoursConfig | None) -> None:
        self._topic_url = topic_url
        self._qh = quiet_hours  # None = no quiet window

    async def send(self, client: httpx.AsyncClient, payload: NotificationPayload) -> None:
        # Quiet hours always wins — even a high-relevance posting goes silent.
        if self._qh is not None and _is_quiet(datetime.now(tz=timezone.utc), self._qh):
            priority = "low"
        else:
            # ntfy priority levels we use: max | default | min (plus low for quiet)
            priority = payload.relevance_priority

        body_lines = [
            f"New posting from {payload.source} · {payload.posted}",
        ]
        if payload.relevance_score is not None and payload.relevance_rationale:
            body_lines.append(f"Relevance: {payload.relevance_score}/10 — {payload.relevance_rationale}")
        elif payload.relevance_rationale:
            # fallback case: score is None but rationale carries "(LLM unavailable)"
            body_lines.append(f"Relevance: {payload.relevance_rationale}")
        if payload.stack_matched:
            body_lines.append(f"Stack matched: {', '.join(s.title() for s in payload.stack_matched)}")
        if payload.comp:
            body_lines.append(f"Comp: {payload.comp}")
        body_lines.append(f"Location: {payload.location}")
        if payload.source == "adzuna":
            body_lines.append("Jobs by Adzuna — https://www.adzuna.com")
        body = "\n".join(body_lines)

        score_prefix = ""
        if payload.relevance_score is not None:
            score_prefix = f"[{payload.relevance_score}/10] "
        elif payload.relevance_rationale == "(LLM unavailable)":
            score_prefix = "[?/10] "

        title = f"{score_prefix}{payload.title}"
        if payload.comp:
            title = f"{title} ({payload.comp})"
        title_with_location = f"{title} - {payload.location}"

        # ntfy passes Title via an HTTP header; httpx rejects non-latin-1 bytes.
        # HN postings (and some descriptions) include en-dash, em-dash, smart
        # quotes, etc. Replace with ASCII-safe equivalents; the body still
        # carries the full UTF-8 content.
        title_ascii = title_with_location.encode("ascii", "replace").decode("ascii")

        headers = {
            "Title": title_ascii,
            "Tags": ",".join(payload.tags) if payload.tags else "",
            "Click": payload.apply_url,
            "Priority": priority,
        }
        if payload.tailor_url:
            # ntfy action button: tap opens the hosted tailor endpoint. ASCII label —
            # the ntfy header is latin-1 (same constraint as Title).
            headers["Actions"] = f"view, Tailor resume, {payload.tailor_url}"
        resp = await client.post(self._topic_url, content=body.encode("utf-8"), headers=headers, timeout=15.0)
        resp.raise_for_status()
