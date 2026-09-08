from __future__ import annotations

import logging
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

from src.models import ConnectorState, FetchResult

log = logging.getLogger(__name__)

# Trip after this many consecutive permanent-dead (404/410) cycles. A module
# constant (not a config knob) by design — promotable later if it needs tuning.
DEAD_AFTER_CYCLES = 3

# How long to skip a connector after a 429 with no usable Retry-After header.
# 15 min ≈ one slow-tier cycle, enough for a rate-limit window to reset.
DEFAULT_BACKOFF_SECONDS = 15 * 60

# HTTP statuses meaning the board is permanently gone (vs. transiently failing).
_DEAD_STATUSES = frozenset({404, 410})
_RATE_LIMIT_STATUS = 429
# Statuses meaning the endpoint is reachable but refusing us: a bot challenge,
# a WAF rule, a revoked key. Distinct from 'transient' because these do NOT
# clear on their own — hiring.cafe put a Cloudflare challenge up on 2026-07-14
# and every poll 403'd for 13 days while 'transient' silently no-oped.
_BLOCKED_STATUSES = frozenset({401, 403})


def classify_outcome(res: object) -> str:
    """Map a connector fetch result/exception to a poll-health outcome:
    'ok' (a FetchResult — includes 304 not-modified), 'dead' (permanent 404/410),
    'blocked' (401/403 — reachable but refusing us), 'rate_limited' (429 — back
    off temporarily), or 'transient' (any other failure — timeout, 5xx, connect,
    protocol)."""
    if isinstance(res, FetchResult):
        return "ok"
    if isinstance(res, httpx.HTTPStatusError):
        code = res.response.status_code
        if code in _DEAD_STATUSES:
            return "dead"
        if code in _BLOCKED_STATUSES:
            return "blocked"
        if code == _RATE_LIMIT_STATUS:
            return "rate_limited"
    return "transient"


def retry_after_seconds(res: object) -> int | None:
    """Parse a 429 response's Retry-After header into seconds, or None if absent
    or unparseable. Retry-After is either an integer second-count or an HTTP-date."""
    if not isinstance(res, httpx.HTTPStatusError):
        return None
    raw = (res.response.headers.get("retry-after") or "").strip()
    if not raw:
        return None
    if raw.isdigit():
        return int(raw)
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0, int((when - datetime.now(timezone.utc)).total_seconds()))


def update_poll_health(
    health,
    outcomes,
    tracked,
    *,
    now_ms: int = 0,
    retry_after: dict[str, int | None] | None = None,
    suppress_dead: bool = True,
    threshold: int = DEAD_AFTER_CYCLES,
) -> None:
    """Apply per-connector outcomes to the circuit breaker.

    ``outcomes`` is a list of ``(connector_name, outcome)``; ``tracked`` is the
    set of names that currently have a health row (read once by the caller) so
    the 'ok' path is write-on-change. 'dead' increments the streak and suppresses
    at ``threshold`` (only when ``suppress_dead`` — the permanent-suppression
    path is ats-only, since recovery re-probes ats connectors). 'blocked' also
    increments the streak, on EVERY tier: unlike 'dead' this is about an
    operator refusing us rather than a board disappearing, and it needs to be
    visible in connector_health wherever it happens — but it never
    auto-suppresses, because a WAF rule or challenge can be lifted as easily as
    it was applied. 'rate_limited' backs the connector off until ``now_ms`` +
    its Retry-After (or DEFAULT_BACKOFF_SECONDS); ``retry_after`` maps name →
    parsed seconds. 'transient' is a no-op; 'ok' clears a tracked connector's
    row. Fail-soft: a store error for one connector is logged, never raised —
    the cycle's correctness never depends on health writes."""
    retry_after = retry_after or {}
    for name, outcome in outcomes:
        try:
            if outcome == "ok":
                if name in tracked:
                    health.clear(name)
            elif outcome == "dead":
                if suppress_dead:
                    streak = health.record_dead(name)
                    if streak >= threshold:
                        health.mark_suppressed(name)
            elif outcome == "blocked":
                streak = health.record_dead(name)
                if streak == threshold:
                    log.warning(
                        "connector_blocked",
                        extra={"connector": name, "streak": streak,
                               "detail": "401/403 for consecutive cycles — "
                                         "bot challenge, WAF rule, or revoked credential"},
                    )
            elif outcome == "rate_limited":
                secs = retry_after.get(name) or DEFAULT_BACKOFF_SECONDS
                health.mark_backoff(name, now_ms + secs * 1000)
            # 'transient' → no-op
        except Exception:  # noqa: BLE001 — health is best-effort; never break the cycle
            log.warning("poll_health_update_failed", extra={"connector": name, "outcome": outcome})


async def recover_suppressed(*, cfg, discovered, boards=None, health, client) -> None:
    """Re-probe currently-suppressed ats connectors (run on the discovery tier).
    A connector that now fetches successfully is cleared (rejoins polling next
    cycle); one still 404/410-ing, or failing transiently, stays suppressed.

    Uses the real connector ``fetch`` (not discovery's ``_probe_one_ats``)
    because that is the only thing that can re-probe Workday tenants — a dead
    Workday tenant must be recoverable too."""
    suppressed = health.suppressed_names()
    if not suppressed:
        return
    # Rebuild every ats connector (no suppression filter) so the suppressed ones
    # can be re-probed with their real fetch logic.
    from src.connectors.base import build_connectors

    conns = build_connectors(cfg, "ats", discovered=discovered, boards=boards)
    built_names = {c.name for c in conns}
    recovered = 0
    for conn in conns:
        if conn.name not in suppressed:
            continue
        try:
            await conn.fetch(client, ConnectorState())
        except Exception:  # noqa: BLE001 — still failing (dead or transient) → stay suppressed
            continue
        health.clear(conn.name)
        recovered += 1
    # Drop orphaned rows: a connector that was suppressed but is no longer built
    # (its slug was removed from config, or a discovered row expired) can never be
    # re-probed or recovered — clear it rather than leave a phantom in the panel.
    orphaned = 0
    for name in suppressed - built_names:
        health.clear(name)
        orphaned += 1
    log.info(
        "poll_health_recovery_done",
        extra={"suppressed": len(suppressed), "recovered": recovered, "orphaned": orphaned},
    )
