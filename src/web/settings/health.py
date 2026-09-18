"""What the runtime stores say about a configured board.

connector_health holds a row only while a connector is unhealthy, so the
absence of a row means healthy — that is why this cannot be written as a
lookup with a 'missing means unknown' default. discovered_slugs is the only
place a posting count lives, and it only has rows for slugs discovery has
probed, so a count is genuinely optional.

Every read is fail-soft: a settings page must render even when a telemetry
table is locked (the macOS host/container lock split makes that a real case)."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)

HEALTHY = "healthy"


@dataclass(frozen=True)
class BoardStatus:
    state: str              # healthy | failing | paused | suppressed
    detail: str
    postings: int | None = None


class _Statuses(dict):
    """A board with no row anywhere is healthy, which is the common case."""

    def __missing__(self, key: str) -> BoardStatus:
        return BoardStatus(HEALTHY, "no problems reported")


def board_status(stores) -> dict[str, BoardStatus] | None:
    """Every board name any store has an opinion on, joined into one status
    each. Returns None (never raises) when a store can't be read at all — the
    caller renders "status unavailable" once instead of a per-row error."""
    try:
        suppressed = set(stores.health.suppressed_names())
        backoff = set(stores.health.backoff_names(int(time.time() * 1000)))
        tracked = set(stores.health.tracked_names())
        counts = {r.connector_name: r for r in stores.discovered.list_all()}
    except Exception as exc:  # noqa: BLE001 — telemetry never breaks a settings page
        log.warning("board_status_unavailable", extra={"error": str(exc)})
        return None

    out = _Statuses()
    for name in suppressed | backoff | tracked | set(counts):
        row = counts.get(name)
        postings = row.last_posting_count if row is not None else None
        if name in suppressed:
            out[name] = BoardStatus("suppressed", "not polled — the board returned 404/410 repeatedly", postings)
        elif name in backoff:
            out[name] = BoardStatus("paused", "rate limited — polling resumes automatically", postings)
        elif name in tracked:
            out[name] = BoardStatus("failing", "recent fetches failed", postings)
        else:
            out[name] = BoardStatus(HEALTHY, "no problems reported", postings)
    return out
