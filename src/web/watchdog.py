"""Pipeline-stopped watchdog. Runs inside the web container (a separate
process from the poller, so it survives poller death) and checks the age of
the newest pipeline_events row. Whole-box death is covered by the optional
outbound heartbeat instead (handler._ping_heartbeat) — no local watchdog can
report its own host dying.

Inert unless an ops sink env var is set AND the local events/alert-state
stores are wired — checked at registration time, so ineligible apps (the
test suite, apps that don't wire the events/alert-state stores, etc.) never
register the on_event handlers at all, avoiding both the loop and the
on_event DeprecationWarning noise."""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

import httpx
import yaml
from fastapi import FastAPI

from src.notify.ops import send_ops_alert
from src.ops_alerts import OpsAlertEvaluator, OpsThresholds

log = logging.getLogger(__name__)

_CHECK_INTERVAL_SECONDS = 300


def _read_yaml_block(config_path: str) -> dict:
    try:
        return yaml.safe_load(Path(config_path).read_text()) or {}
    except (OSError, yaml.YAMLError):
        return {}


def _ats_minutes(config_path: str) -> int:
    """schedules.ats_minutes without requiring notification secrets (same
    reasoning as web.__main__._score_thresholds)."""
    try:
        return int((_read_yaml_block(config_path).get("schedules") or {}).get("ats_minutes", 5))
    except (ValueError, TypeError):
        return 5


# Tiers that run on a fixed interval and therefore have a meaningful staleness
# threshold. discovery/digest are excluded on purpose: daily/weekly cadences
# make "overdue" a much weaker signal, and the tiers that actually deliver
# postings are these three.
_INTERVAL_TIER_KEYS = {"ats": "ats_minutes", "slow": "slow_minutes",
                       "headless": "headless_minutes"}
_INTERVAL_TIER_DEFAULTS = {"ats": 5, "slow": 15, "headless": 45}


def _tier_intervals(config_path: str) -> dict[str, int]:
    """Per-tier cadence in minutes, for the per-tier staleness watchdog."""
    blk = _read_yaml_block(config_path).get("schedules") or {}
    out: dict[str, int] = {}
    for tier, key in _INTERVAL_TIER_KEYS.items():
        try:
            out[tier] = int(blk.get(key, _INTERVAL_TIER_DEFAULTS[tier]))
        except (ValueError, TypeError):
            out[tier] = _INTERVAL_TIER_DEFAULTS[tier]
    return out


def _thresholds(config_path: str) -> OpsThresholds:
    blk = _read_yaml_block(config_path).get("ops_notify") or {}
    try:
        return OpsThresholds(
            llm_degraded_cycles=int(blk.get("llm_degraded_cycles", 2)),
            zero_yield_hours=int(blk.get("zero_yield_hours", 12)),
            cooldown_hours=int(blk.get("cooldown_hours", 6)),
        )
    except (ValueError, TypeError):
        return OpsThresholds()


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


def register_watchdog(app: FastAPI) -> None:
    """Register the on_event handlers only if the watchdog is eligible to run
    — otherwise skip registration entirely so the @app.on_event
    DeprecationWarning never fires for ineligible apps (the test suite, apps
    with no ops sink configured, etc.)."""
    ntfy = os.environ.get("JOB_AGG_OPS_NTFY_TOPIC_URL", "")
    discord = os.environ.get("JOB_AGG_OPS_DISCORD_WEBHOOK_URL", "")
    stores = getattr(app.state, "stores", None)
    events = getattr(stores, "events", None)
    alert_state = getattr(stores, "alert_state", None)
    if not (ntfy or discord) or events is None or alert_state is None:
        return
    cfg_path = os.environ.get("JOB_AGG_CONFIG_PATH", "config.yaml")
    ats = _ats_minutes(cfg_path)
    thresholds = _thresholds(cfg_path)
    tier_intervals = _tier_intervals(cfg_path)

    @app.on_event("startup")
    async def _start() -> None:
        async def _loop() -> None:
            while True:
                try:
                    await check_once(
                        events=events, alert_state=alert_state, ats_minutes=ats,
                        thresholds=thresholds, ntfy_topic_url=ntfy,
                        discord_webhook_url=discord, tier_intervals=tier_intervals,
                    )
                except Exception:  # noqa: BLE001 — the loop must survive any check failure
                    log.warning("watchdog_check_failed")
                await asyncio.sleep(_CHECK_INTERVAL_SECONDS)

        app.state.watchdog_task = asyncio.create_task(_loop())
        log.info("watchdog_started", extra={"ats_minutes": ats})

    @app.on_event("shutdown")
    async def _stop() -> None:
        task = getattr(app.state, "watchdog_task", None)
        if task is not None:
            task.cancel()
