"""Long-running scheduler daemon for the local (Docker Compose) runtime.

Mirrors the four EventBridge rules — it calls the same handler._run(tier)
entrypoint the Lambda uses, on cadences read from config.schedules. Run as
`python -m src.scheduler`. The Lambda path is unaffected.

A fifth job (`prune`) runs daily at 04:00 UTC and calls
SqliteSeenJobsStore.prune_expired() to remove expired rows that DynamoDB
would handle via native TTL. The DynamoDB store does not have this method;
the job is a no-op in that backend."""

from __future__ import annotations

import asyncio
import logging
import os

import httpx
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from src.board_digest import compose_board_digest, send_board_digest
from src.closed_check import run_closed_sweep
from src.config import AppConfig, load_config
from src.gmail_ingest import run_gmail_check
from src.handler import _run
from src.logging_setup import configure_logging
from src.stores import build_stores

log = logging.getLogger(__name__)


def _tick(tier: str) -> None:
    """Run one tier cycle. Each job is sync (APScheduler default executor); the
    async pipeline is driven with asyncio.run so a slow cycle blocks only its
    own job thread, not the scheduler."""
    try:
        asyncio.run(_run(tier=tier))
    except Exception:  # noqa: BLE001 — never let one bad cycle kill the daemon
        log.exception("scheduled_cycle_failed", extra={"tier": tier})


def _integrity_check() -> None:
    """Hourly SQLite self-heal: detect corruption and REINDEX it away before it
    silently kills a tier (a corrupt source_state index once took the ats tier
    down for days). Poller-side on purpose — the process that writes the repair
    then reads the DB, so it sees the fix immediately (the web container's repair
    wasn't visible to the poller until a restart; see sqlite_db.connect()).

    SQLite-backend only; a no-op otherwise. Never re-raised — keeps the daemon up."""
    if os.environ.get("JOB_AGG_BACKEND", "sqlite") != "sqlite":
        return
    try:
        from src.sqlite_db import connect, integrity_check_and_repair

        conn = connect()
        try:
            integrity_check_and_repair(conn)
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — maintenance must never kill the daemon
        log.exception("scheduled_integrity_check_failed")


def _prune(cfg: AppConfig) -> None:
    """Daily maintenance: remove expired seen-jobs rows (SQLite lacks DynamoDB's
    native TTL) and sweep the audit trail past its retention window. Each part
    no-ops on backends that don't wire the store; failures are logged, never
    re-raised, so the daemon keeps running."""
    try:
        stores = build_stores(cfg)
        prune = getattr(stores.seen, "prune_expired", None)
        if prune:
            removed = prune()
            log.info("scheduled_prune_complete", extra={"removed": removed})
        else:
            log.debug("scheduled_prune_skipped: store has no prune_expired")
        if stores.rejected is not None and cfg.audit.enabled:
            removed = stores.rejected.prune_older_than(cfg.audit.retention_days)
            log.info("scheduled_rejected_prune_complete", extra={"removed": removed})
    except Exception:  # noqa: BLE001
        log.exception("scheduled_prune_failed")


def _closed_check(cfg: AppConfig) -> None:
    """Daily posting-closed sweep over active board cards. Fail-soft: a
    failure is logged and the daemon keeps running."""
    try:
        stores = build_stores(cfg)
        result = asyncio.run(run_closed_sweep(stores.seen))
        log.info("scheduled_closed_check_complete", extra=result)
    except Exception:  # noqa: BLE001
        log.exception("scheduled_closed_check_failed")


def _board_digest(cfg: AppConfig) -> None:
    """Daily stale/closed digest on the job channel. closed_notified is
    marked only after a sink accepted the send, so a total failure
    re-announces tomorrow instead of losing the event."""
    try:
        stores = build_stores(cfg)
        cards = stores.seen.list_matches()
        result = compose_board_digest(cards, stale_after_days=cfg.board.stale_after_days)
        if result is None:
            log.info("scheduled_board_digest_empty")
            return
        message, to_mark = result
        click = f"{cfg.board.web_base_url.rstrip('/')}/board" if cfg.board.web_base_url else ""

        async def _send() -> bool:
            async with httpx.AsyncClient() as client:
                return await send_board_digest(
                    client, message,
                    ntfy_topic_url=cfg.secrets.ntfy_topic_url,
                    discord_webhook_url=cfg.secrets.discord_webhook_url,
                    click_url=click,
                )

        sent = asyncio.run(_send())
        mark = getattr(stores.seen, "mark_closed_notified", None)
        if sent and mark:
            for job_id in to_mark:
                mark(job_id)
        log.info("scheduled_board_digest_done", extra={"sent": sent, "closed_marked": len(to_mark) if sent else 0})
    except Exception:  # noqa: BLE001
        log.exception("scheduled_board_digest_failed")


def _gmail_check(cfg: AppConfig) -> None:
    """Hourly read-only Gmail sweep writing suggest-only board badges.
    Env-gated on the two JOB_AGG_GMAIL_* secrets; fail-soft: a failure is
    logged, the watermark stays put, and the daemon keeps running."""
    try:
        if not (cfg.secrets.gmail_address and cfg.secrets.gmail_app_password):
            log.debug("scheduled_gmail_check_skipped: gmail secrets not configured")
            return
        stores = build_stores(cfg)
        result = run_gmail_check(
            stores.seen, stores.source_state,
            address=cfg.secrets.gmail_address,
            app_password=cfg.secrets.gmail_app_password,
            first_run_days=cfg.gmail.first_run_days,
            lookback_max_days=cfg.gmail.lookback_max_days,
            max_messages=cfg.gmail.max_messages_per_run,
        )
        log.info("scheduled_gmail_check_complete", extra=result)
    except Exception:  # noqa: BLE001
        log.exception("scheduled_gmail_check_failed")


def build_scheduler(cfg: AppConfig) -> BlockingScheduler:
    sched = BlockingScheduler(timezone="UTC")
    sched.add_job(
        _tick, IntervalTrigger(minutes=cfg.schedules.ats_minutes),
        args=["ats"], id="ats", max_instances=1, coalesce=True,
    )
    sched.add_job(
        _tick, IntervalTrigger(minutes=cfg.schedules.slow_minutes),
        args=["slow"], id="slow", max_instances=1, coalesce=True,
    )
    sched.add_job(
        _tick, IntervalTrigger(hours=cfg.schedules.discovery_hours),
        args=["discovery"], id="discovery", max_instances=1, coalesce=True,
    )
    sched.add_job(
        _tick, IntervalTrigger(minutes=cfg.schedules.headless_minutes),
        args=["headless"], id="headless", max_instances=1, coalesce=True,
    )
    sched.add_job(
        _tick, CronTrigger.from_crontab(cfg.schedules.digest_cron, timezone="UTC"),
        args=["digest"], id="digest", max_instances=1, coalesce=True,
    )
    sched.add_job(
        _prune, CronTrigger.from_crontab("0 4 * * *", timezone="UTC"),
        args=[cfg], id="prune", max_instances=1, coalesce=True,
    )
    sched.add_job(
        _closed_check, CronTrigger.from_crontab(cfg.board.closed_check_cron, timezone="UTC"),
        args=[cfg], id="closed_check", max_instances=1, coalesce=True,
    )
    sched.add_job(
        _board_digest, CronTrigger.from_crontab(cfg.board.digest_cron, timezone="UTC"),
        args=[cfg], id="board_digest", max_instances=1, coalesce=True,
    )
    sched.add_job(
        _gmail_check, CronTrigger.from_crontab(cfg.gmail.check_cron, timezone="UTC"),
        args=[cfg], id="gmail_check", max_instances=1, coalesce=True,
    )
    sched.add_job(
        _integrity_check, IntervalTrigger(hours=1),
        id="integrity", max_instances=1, coalesce=True,
    )
    return sched


def main() -> int:
    configure_logging()
    cfg = load_config(os.environ.get("JOB_AGG_CONFIG_PATH", "config.yaml"))
    sched = build_scheduler(cfg)
    log.info("scheduler_starting", extra={
        "ats_minutes": cfg.schedules.ats_minutes,
        "slow_minutes": cfg.schedules.slow_minutes,
        "discovery_hours": cfg.schedules.discovery_hours,
        "digest_cron": cfg.schedules.digest_cron,
    })
    sched.start()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
