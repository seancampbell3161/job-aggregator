"""Long-running scheduler daemon: runs handler._run(tier) on cadences read
from the settings service. Run as `python -m src.scheduler`.

Every job is registered at boot whether or not the instance is set up:
triggers come from the current settings, or the model defaults. Each job takes
its own snapshot when it fires and no-ops until setup. config_watch re-applies
trigger changes within CONFIG_WATCH_SECONDS of a settings save — no restart.
A daily `prune` job (04:00 UTC) removes TTL-expired rows."""

from __future__ import annotations

import asyncio
import logging

import httpx
from apscheduler.schedulers.base import BaseScheduler
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from src.board_digest import compose_board_digest, send_board_digest
from src.closed_check import run_closed_sweep
from src.config import AppConfig
from src.gmail_ingest import run_gmail_check
from src.handler import _run
from src.logging_setup import configure_logging
from src.settings.service import ConfigService, ConfigSnapshot
from src.stores import build_stores

log = logging.getLogger(__name__)

CONFIG_WATCH_SECONDS = 30

# (kind, value): kind is an IntervalTrigger unit ("minutes"/"hours") or "cron".
# Comparing specs is how config_watch spots a changed trigger.
TriggerSpec = tuple[str, int | str]


def _trigger_specs(cfg: AppConfig) -> dict[str, TriggerSpec]:
    return {
        "ats": ("minutes", cfg.schedules.ats_minutes),
        "slow": ("minutes", cfg.schedules.slow_minutes),
        "discovery": ("hours", cfg.schedules.discovery_hours),
        "headless": ("minutes", cfg.schedules.headless_minutes),
        "digest": ("cron", cfg.schedules.digest_cron),
        "closed_check": ("cron", cfg.board.closed_check_cron),
        "board_digest": ("cron", cfg.board.digest_cron),
        "gmail_check": ("cron", cfg.gmail.check_cron),
    }


def _make_trigger(spec: TriggerSpec):
    kind, value = spec
    if kind == "cron":
        return CronTrigger.from_crontab(str(value), timezone="UTC")
    return IntervalTrigger(**{kind: int(value)})


def _snapshot_or_skip(service: ConfigService, job: str) -> ConfigSnapshot | None:
    snap = service.snapshot()
    if snap is None:
        log.info("awaiting_setup", extra={"job": job})
    return snap


def _tick(service: ConfigService, tier: str) -> None:
    """Run one tier cycle. Each job is sync (APScheduler default executor); the
    async pipeline is driven with asyncio.run so a slow cycle blocks only its
    own job thread, not the scheduler."""
    try:
        asyncio.run(_run(tier=tier, service=service))
    except Exception:  # noqa: BLE001 — never let one bad cycle kill the daemon
        log.exception("scheduled_cycle_failed", extra={"tier": tier})


def _integrity_check() -> None:
    """Hourly SQLite self-heal: detect corruption and REINDEX it away before it
    silently kills a tier (a corrupt source_state index once took the ats tier
    down for days). Poller-side on purpose — the process that writes the repair
    then reads the DB, so it sees the fix immediately (the web container's repair
    wasn't visible to the poller until a restart; see sqlite_db.connect()).

    Never re-raised — keeps the daemon up."""
    try:
        from src.sqlite_db import connect, integrity_check_and_repair

        conn = connect()
        try:
            integrity_check_and_repair(conn)
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — maintenance must never kill the daemon
        log.exception("scheduled_integrity_check_failed")


def _prune(service: ConfigService) -> None:
    """Daily maintenance: remove expired seen-jobs rows and sweep the audit
    trail past its retention window. Failures are logged, never re-raised,
    so the daemon keeps running."""
    try:
        snap = _snapshot_or_skip(service, "prune")
        if snap is None:
            return
        cfg = snap.cfg
        stores = build_stores()
        removed = stores.seen.prune_expired()
        log.info("scheduled_prune_complete", extra={"removed": removed})
        if cfg.audit.enabled:
            removed = stores.rejected.prune_older_than(cfg.audit.retention_days)
            log.info("scheduled_rejected_prune_complete", extra={"removed": removed})
    except Exception:  # noqa: BLE001
        log.exception("scheduled_prune_failed")


def _closed_check(service: ConfigService) -> None:
    """Daily posting-closed sweep over active board cards. Fail-soft: a
    failure is logged and the daemon keeps running."""
    try:
        if _snapshot_or_skip(service, "closed_check") is None:
            return
        stores = build_stores()
        result = asyncio.run(run_closed_sweep(stores.seen))
        log.info("scheduled_closed_check_complete", extra=result)
    except Exception:  # noqa: BLE001
        log.exception("scheduled_closed_check_failed")


def _board_digest(service: ConfigService) -> None:
    """Daily stale/closed digest on the job channel. closed_notified is
    marked only after a sink accepted the send, so a total failure
    re-announces tomorrow instead of losing the event."""
    try:
        snap = _snapshot_or_skip(service, "board_digest")
        if snap is None:
            return
        cfg = snap.cfg
        stores = build_stores()
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
        if sent:
            for job_id in to_mark:
                stores.seen.mark_closed_notified(job_id)
        log.info("scheduled_board_digest_done",
                 extra={"sent": sent, "closed_marked": len(to_mark) if sent else 0})
    except Exception:  # noqa: BLE001
        log.exception("scheduled_board_digest_failed")


def _gmail_check(service: ConfigService) -> None:
    """Hourly read-only Gmail sweep writing suggest-only board badges.
    Gated on the two gmail secrets; fail-soft: a failure is logged, the
    watermark stays put, and the daemon keeps running."""
    try:
        snap = _snapshot_or_skip(service, "gmail_check")
        if snap is None:
            return
        cfg = snap.cfg
        if not (cfg.secrets.gmail_address and cfg.secrets.gmail_app_password):
            log.debug("scheduled_gmail_check_skipped: gmail secrets not configured")
            return
        stores = build_stores()
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


def _config_watch(sched: BaseScheduler, service: ConfigService, applied: dict[str, TriggerSpec]) -> None:
    """Re-apply schedule triggers whose settings changed. ``applied`` holds the
    spec each job currently runs on; only jobs whose spec differs are
    rescheduled, and a failed reschedule is retried on the next pass."""
    try:
        snap = service.snapshot()
        cfg = snap.cfg if snap is not None else AppConfig()
        for job_id, spec in _trigger_specs(cfg).items():
            if applied.get(job_id) == spec:
                continue
            try:
                sched.reschedule_job(job_id, trigger=_make_trigger(spec))
            except Exception:  # noqa: BLE001 — one bad job must not block the rest
                log.exception("job_reschedule_failed", extra={"job": job_id})
                continue
            applied[job_id] = spec
            log.info("job_rescheduled", extra={"job": job_id, "trigger": f"{spec[0]}={spec[1]}"})
    except Exception:  # noqa: BLE001 — the watch must never kill the daemon
        log.exception("config_watch_failed")


def _boot_config(service: ConfigService) -> AppConfig:
    """Settings for the initial triggers: the snapshot, or model defaults when
    not set up or the DB is unreadable at boot (config_watch catches up)."""
    try:
        snap = service.snapshot()
    except Exception:  # noqa: BLE001
        log.exception("scheduler_boot_snapshot_failed")
        return AppConfig()
    return snap.cfg if snap is not None else AppConfig()


def build_scheduler(service: ConfigService) -> BlockingScheduler:
    sched = BlockingScheduler(timezone="UTC")
    specs = _trigger_specs(_boot_config(service))
    for tier in ("ats", "slow", "discovery", "headless", "digest"):
        sched.add_job(_tick, _make_trigger(specs[tier]), args=[service, tier],
                      id=tier, max_instances=1, coalesce=True)
    for job_id, func in (("closed_check", _closed_check), ("board_digest", _board_digest),
                         ("gmail_check", _gmail_check)):
        sched.add_job(func, _make_trigger(specs[job_id]), args=[service],
                      id=job_id, max_instances=1, coalesce=True)
    sched.add_job(_prune, CronTrigger.from_crontab("0 4 * * *", timezone="UTC"), args=[service],
                  id="prune", max_instances=1, coalesce=True)
    sched.add_job(_integrity_check, IntervalTrigger(hours=1),
                  id="integrity", max_instances=1, coalesce=True)
    sched.add_job(_config_watch, IntervalTrigger(seconds=CONFIG_WATCH_SECONDS),
                  args=[sched, service, dict(specs)],
                  id="config_watch", max_instances=1, coalesce=True)
    return sched


def main() -> int:
    configure_logging()
    service = ConfigService(build_stores().settings)
    try:
        service.ensure_signing_secret()
    except Exception:  # noqa: BLE001 — deep-link signing degrades; polling must still start
        log.exception("tailor_signing_secret_bootstrap_failed")
    sched = build_scheduler(service)
    log.info("scheduler_starting", extra={"jobs": sorted(j.id for j in sched.get_jobs())})
    sched.start()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
