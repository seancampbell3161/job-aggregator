"""Daily board digest: stale cards + newly-closed postings, one message on
the job-alert channel, sent only when non-empty. Closures are announced once
(the caller marks closed_notified after a successful send); staleness nags
daily by design."""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone

import httpx

log = logging.getLogger(__name__)

_STALE_STATUSES = ("applied", "interviewing")
_ACTIVE_STATUSES = ("interested", "applied", "interviewing", "offer")
_DISCORD_CONTENT_MAX = 2000


def _current_since(card: dict) -> str:
    history = card.get("history") or []
    if history:
        at = history[-1].get("at", "")
        if at:
            return at[:10]
    return (card.get("first_seen") or "")[:10]


def _days_in_stage(card: dict, now: datetime) -> int:
    try:
        since = date.fromisoformat(_current_since(card))
    except (ValueError, TypeError):
        return 0
    return (now.date() - since).days


def _applied_date_label(card: dict) -> str:
    """'6/20' from the card's first 'applied' history entry, else ''. """
    for e in card.get("history") or []:
        if e.get("status") == "applied" and e.get("at"):
            try:
                d = date.fromisoformat(e["at"][:10])
                return f"{d.month}/{d.day}"
            except (ValueError, TypeError):
                return ""
    return ""


def compose_board_digest(
    cards: list[dict], *, stale_after_days: int, now: datetime | None = None,
) -> tuple[str, list[str]] | None:
    """(message, job_ids_to_mark_notified) or None when there's nothing to say."""
    now = now or datetime.now(timezone.utc)

    stale = [
        c for c in cards
        if c.get("status") in _STALE_STATUSES
        and _days_in_stage(c, now) >= stale_after_days
    ]
    newly_closed = [
        c for c in cards
        if c.get("posting_closed_at") and not c.get("closed_notified")
    ]
    pending = [
        c for c in cards
        if c.get("email_suggestion") and c.get("status") in _ACTIVE_STATUSES
    ]
    if not stale and not newly_closed and not pending:
        return None

    parts: list[str] = []
    if stale:
        stale.sort(key=lambda c: _days_in_stage(c, now), reverse=True)
        items = ", ".join(
            f"{c.get('company', '?')} ({_days_in_stage(c, now)}d {c.get('status')})"
            for c in stale
        )
        parts.append(f"{len(stale)} stale — {items}")
    if newly_closed:
        items = ", ".join(
            f"{c.get('company', '?')}"
            + (f" (applied {label})" if (label := _applied_date_label(c)) else "")
            for c in newly_closed
        )
        parts.append(f"{len(newly_closed)} posting closed: {items}")
    if pending:
        n = len(pending)
        parts.append(f"{n} email suggestion{'s' if n != 1 else ''} pending")

    message = "Board: " + " · ".join(parts)
    return message, [c["job_id"] for c in newly_closed]


async def send_board_digest(
    client: httpx.AsyncClient, content: str, *,
    ntfy_topic_url: str = "", discord_webhook_url: str = "", click_url: str = "",
) -> bool:
    """Send to the job channel's sinks. True iff >=1 accepted; per-sink
    fail-soft (mirrors src/notify/ops.py)."""
    ok = False
    if ntfy_topic_url:
        try:
            headers = {
                "Title": "Board digest".encode("ascii", "replace").decode("ascii"),
                "Priority": "default",
                "Tags": "clipboard",
            }
            if click_url:
                headers["Click"] = click_url
            resp = await client.post(
                ntfy_topic_url, content=content.encode("utf-8"),
                headers=headers, timeout=15.0,
            )
            resp.raise_for_status()
            ok = True
        except Exception as exc:  # noqa: BLE001 — digest is best-effort; tomorrow retries
            log.warning("board_digest_ntfy_failed", extra={"error": str(exc)})
    if discord_webhook_url:
        try:
            resp = await client.post(
                discord_webhook_url,
                json={"content": f"📋 {content}"[:_DISCORD_CONTENT_MAX]},
                timeout=15.0,
            )
            resp.raise_for_status()
            ok = True
        except Exception as exc:  # noqa: BLE001
            log.warning("board_digest_discord_failed", extra={"error": str(exc)})
    return ok
