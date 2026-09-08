"""Aggregate résumé-gap reporting: tally recurring skill gaps across recently
notified jobs and format them for the weekly Discord digest / CLI report."""

from __future__ import annotations

import logging
from collections import Counter

import httpx

log = logging.getLogger(__name__)

_TOP_N = 10
_MIN_COUNT = 2                  # drop singletons — one appearance isn't a signal
_DISCORD_CONTENT_MAX = 2000     # Discord message content hard limit


def tally_gaps(lists: list[list[str]], *, min_count: int = _MIN_COUNT, top: int = _TOP_N) -> list[tuple[str, int]]:
    """Count how many jobs each skill was missing from. Case-insensitive (the
    first-seen display form is preserved), deduped within a job. ``min_count``
    drops rarer skills (default 2 → singletons dropped, as the digest wants;
    the analytics UI passes 1). ``top`` caps the result length."""
    counts: Counter = Counter()
    display: dict[str, str] = {}
    for lst in lists:
        seen_in_job: set[str] = set()
        for raw in lst:
            name = raw.strip()
            key = name.lower()
            if not key or key in seen_in_job:
                continue
            seen_in_job.add(key)
            counts[key] += 1
            display.setdefault(key, name)
    ranked = [(display[k], c) for k, c in counts.most_common() if c >= min_count]
    return ranked[:top]


def format_gap_digest(tally: list[tuple[str, int]], *, window_days: int, total_jobs: int) -> str:
    """Render the digest message body. total_jobs is the count of notified jobs
    in the window (the denominator), not just those with gaps."""
    if total_jobs == 0:
        return f"📊 Gap digest: no matches in the last {window_days} days."
    if not tally:
        return (
            f"📊 Gap digest: across your last {total_jobs} matches "
            f"({window_days} days), no recurring skill gaps — nice."
        )
    items = ", ".join(f"{name} ({count})" for name, count in tally)
    return (
        f"📊 Top stretch skills across your last {total_jobs} matches "
        f"({window_days} days): {items}"
    )


async def send_gap_digest(client: httpx.AsyncClient, webhook_url: str, content: str) -> None:
    """Post the digest as a single Discord webhook message. Raises on HTTP error."""
    resp = await client.post(webhook_url, json={"content": content[:_DISCORD_CONTENT_MAX]}, timeout=15.0)
    if resp.status_code >= 400:
        log.warning("gap_digest_post_failed", extra={"status_code": resp.status_code, "body": resp.text[:500]})
    resp.raise_for_status()
