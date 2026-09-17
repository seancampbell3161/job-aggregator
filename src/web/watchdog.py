"""Pipeline-stopped watchdog. Runs inside the web container (a separate
process from the poller, so it survives poller death) and checks the age of
the newest pipeline_events row. Whole-box death is covered by the optional
outbound heartbeat instead (handler._ping_heartbeat) — no local watchdog can
report its own host dying.

Started by the web app's lifespan whenever the events/alert-state stores are
wired. Each pass reads the ops sink URLs, thresholds, and tier cadences from a
fresh settings snapshot and does nothing until the instance is set up and an
ops sink is configured — so turning ops alerts on later needs no restart."""
from __future__ import annotations

import asyncio
import logging

import httpx
from fastapi import FastAPI

from src.notify.ops import send_ops_alert
from src.ops_alerts import OpsAlertEvaluator, OpsThresholds

log = logging.getLogger(__name__)

_CHECK_INTERVAL_SECONDS = 300


def _tier_intervals(cfg) -> dict[str, int]:
    """Per-tier cadence in minutes for the tiers on a fixed interval.
    discovery/digest are excluded on purpose: daily/weekly cadences make
    "overdue" a much weaker signal, and these three deliver the postings."""
    return {
        "ats": cfg.schedules.ats_minutes,
        "slow": cfg.schedules.slow_minutes,
        "headless": cfg.schedules.headless_minutes,
    }


def _thresholds(cfg) -> OpsThresholds:
    return OpsThresholds(
        llm_degraded_cycles=cfg.ops_notify.llm_degraded_cycles,
        zero_yield_hours=cfg.ops_notify.zero_yield_hours,
        cooldown_hours=cfg.ops_notify.cooldown_hours,
    )


async def check_once(
    *, events, alert_state, ats_minutes: int, thresholds: OpsThresholds,
    ntfy_topic_url: str, discord_webhook_url: str,
    now_ms: int | None = None, client_factory=None,
    tier_intervals: dict[str, int] | None = None,
) -> bool:
    """One staleness check. Returns True iff an alert/recovery was sent.

    Two conditions: the whole pipeline stopping, and any single tier stalling
    while the others keep running (which pipeline_stopped cannot see)."""
    evaluator = OpsAlertEvaluator(state=alert_state, events=events, thresholds=thresholds)
    alerts = []
    whole = evaluator.evaluate_staleness(
        expected_interval_minutes=ats_minutes, now_ms=now_ms
    )
    if whole is not None:
        alerts.append(whole)
    if tier_intervals:
        alerts.extend(
            evaluator.evaluate_tier_staleness(
                tier_intervals=tier_intervals, now_ms=now_ms
            )
        )
    if not alerts:
        return False
    factory = client_factory or (lambda: httpx.AsyncClient())
    any_sent = False
    async with factory() as client:
        for alert in alerts:
            sent = await send_ops_alert(
                client, alert,
                ntfy_topic_url=ntfy_topic_url, discord_webhook_url=discord_webhook_url,
            )
            any_sent = any_sent or sent
            if not sent and not alert.recovered:
                # Every sink failed: re-arm so the next check retries instead of
                # waiting out the cooldown. A lost recovery notice is accepted
                # (re-arming would fake a firing state).
                alert_state.mark_recovered(alert.condition)
    return any_sent


async def watchdog_pass(*, service, events, alert_state, client_factory=None) -> bool:
    """One watchdog iteration against the current settings. Returns True iff an
    alert/recovery was sent; False when not set up or no ops sink is set."""
    snap = await asyncio.to_thread(service.snapshot)
    if snap is None:
        return False
    cfg = snap.cfg
    ntfy, discord = cfg.secrets.ops_ntfy_topic_url, cfg.secrets.ops_discord_webhook_url
    if not (ntfy or discord):
        return False
    return await check_once(
        events=events, alert_state=alert_state, ats_minutes=cfg.schedules.ats_minutes,
        thresholds=_thresholds(cfg), ntfy_topic_url=ntfy, discord_webhook_url=discord,
        tier_intervals=_tier_intervals(cfg), client_factory=client_factory,
    )


def start_watchdog(app: FastAPI) -> asyncio.Task | None:
    """Start the watchdog loop when the app has events + alert-state stores and
    a settings service (every real app does). Returns the task, or None."""
    stores = getattr(app.state, "stores", None)
    events = getattr(stores, "events", None)
    alert_state = getattr(stores, "alert_state", None)
    service = getattr(app.state, "service", None)
    if events is None or alert_state is None or service is None:
        return None

    async def _loop() -> None:
        while True:
            try:
                await watchdog_pass(service=service, events=events, alert_state=alert_state)
            except Exception:  # noqa: BLE001 — the loop must survive any check failure
                log.warning("watchdog_check_failed")
            await asyncio.sleep(_CHECK_INTERVAL_SECONDS)

    task = asyncio.create_task(_loop())
    log.info("watchdog_started")
    return task
