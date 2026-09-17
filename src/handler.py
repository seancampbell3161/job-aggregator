from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from dataclasses import asdict
from typing import Any

import httpx

from src.config import AppConfig
from src.connectors.base import build_connectors
from src.digest import format_gap_digest, send_gap_digest, tally_gaps
from src.discovery import DiscoveryConfig, make_yc_oss_fetcher, run_board_discovery, run_discovery, run_vc_discovery
from src.fingerprint import DEFAULT_SEEDS, EU_SEEDS, load_seeds
from src.poll_health import recover_suppressed
from src.logging_setup import configure_logging
from src.notify.base import Sink
from src.notify.discord import DiscordSink
from src.notify.ntfy import NtfySink
from src.notify.ops import send_ops_alert
from src.ops_alerts import OpsAlertEvaluator, OpsThresholds
from src.orchestrator import run_once
from src.coach import AnthropicCoach, CoachEngine, GeminiCoach, OllamaCoach
from src.gaps import AnthropicGapAnalyzer, GapAnalyzer, GeminiGapAnalyzer, OllamaGapAnalyzer
from src.relevance import GeminiRelevanceScorer, OllamaRelevanceScorer, RelevanceScorer
from src.settings.service import ConfigService
from src.stores import build_stores

log = logging.getLogger(__name__)

_VALID_TIERS = {"ats", "slow", "discovery", "digest", "headless"}


def _build_sinks(cfg: AppConfig) -> list[Sink]:
    """One sink per configured notification URL; an unset URL omits its sink."""
    sinks: list[Sink] = []
    if cfg.secrets.ntfy_topic_url:
        sinks.append(NtfySink(topic_url=cfg.secrets.ntfy_topic_url, quiet_hours=cfg.quiet_hours))
    if cfg.secrets.discord_webhook_url:
        sinks.append(DiscordSink(webhook_url=cfg.secrets.discord_webhook_url))
    return sinks


def _discovery_seeds(eu_seeds_enabled: bool) -> list:
    """Board-sweep seed list: the default (US) file, plus the EU file when
    discovery.eu_seeds_enabled is set. Both files share the same sweep budget
    and quarantine knobs."""
    seeds = load_seeds(DEFAULT_SEEDS)
    if eu_seeds_enabled:
        seeds += load_seeds(EU_SEEDS)
    return seeds


async def _ping_heartbeat(url: str, *, client_factory=None) -> None:
    """Dead-man's-switch ping (healthchecks.io-style): one GET per successful
    ats cycle. Best-effort — a failed ping is logged, never raised."""
    factory = client_factory or (lambda: httpx.AsyncClient(timeout=5.0))
    try:
        async with factory() as client:
            await client.get(url)
    except Exception as exc:  # noqa: BLE001
        log.warning("heartbeat_ping_failed", extra={"error": str(exc)})


def _build_relevance_scorer(
    cfg: AppConfig, profile_text: str | None
) -> RelevanceScorer | GeminiRelevanceScorer | OllamaRelevanceScorer | None:
    """Construct the scorer if relevance is enabled and the matching API key
    is set. Returns None if relevance is disabled, the API key is missing, or
    there is no profile document — the orchestrator passes through with
    score=None in that case."""
    if not cfg.relevance.enabled:
        return None

    provider = cfg.relevance.provider
    if provider == "anthropic":
        api_key = cfg.secrets.anthropic_api_key
        key_name = "anthropic_api_key"
    elif provider == "gemini":
        api_key = cfg.secrets.google_api_key
        key_name = "google_api_key"
    elif provider == "ollama":
        api_key = cfg.secrets.ollama_api_key
        key_name = "ollama_api_key"
    else:  # pragma: no cover — Pydantic Literal prevents this branch
        return None

    needs_key = not (provider == "ollama" and cfg.relevance.ollama_is_local)
    if needs_key and not api_key:
        log.warning(
            "relevance_disabled_at_runtime",
            extra={"reason": f"{key_name} not set", "provider": provider},
        )
        return None

    if profile_text is None:
        # Soft-fail symmetric with a missing API key: no profile document means
        # nothing to grade against, so relevance is off for this run.
        log.warning("relevance_disabled_at_runtime", extra={"reason": "profile document missing"})
        return None

    if provider == "anthropic":
        from anthropic import AsyncAnthropic
        client = AsyncAnthropic(api_key=api_key)
        return RelevanceScorer(
            client=client,
            model=cfg.relevance.model,
            profile_md=profile_text,
            timeout_seconds=cfg.relevance.timeout_seconds,
        )

    if provider == "ollama":
        from ollama import AsyncClient
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        client = AsyncClient(host=cfg.relevance.ollama_host, headers=headers)
        return OllamaRelevanceScorer(
            client=client,
            model=cfg.relevance.model,
            profile_md=profile_text,
            timeout_seconds=cfg.relevance.timeout_seconds,
        )

    # provider == "gemini"
    from google import genai  # type: ignore
    client = genai.Client(api_key=api_key)
    return GeminiRelevanceScorer(
        client=client,
        model=cfg.relevance.model,
        profile_md=profile_text,
        timeout_seconds=cfg.relevance.timeout_seconds,
    )


def _build_gap_analyzer(cfg: AppConfig, resume_text: str | None) -> GapAnalyzer | None:
    """Construct the gap analyzer if gap_analysis is enabled and its inputs are
    present. Returns None (feature inert) when disabled, the provider API key is
    missing, or there is no resume document — symmetric with
    _build_relevance_scorer. Provider/model fall back to the relevance
    settings when unset."""
    if not cfg.gap_analysis.enabled:
        return None

    provider = cfg.gap_analysis.provider or cfg.relevance.provider
    model = cfg.gap_analysis.model or cfg.relevance.model

    if provider == "anthropic":
        api_key, key_name = cfg.secrets.anthropic_api_key, "anthropic_api_key"
    elif provider == "gemini":
        api_key, key_name = cfg.secrets.google_api_key, "google_api_key"
    elif provider == "ollama":
        api_key, key_name = cfg.secrets.ollama_api_key, "ollama_api_key"
    else:  # pragma: no cover — Pydantic Literal prevents this
        return None

    needs_key = not (provider == "ollama" and cfg.relevance.ollama_is_local)
    if needs_key and not api_key:
        log.warning(
            "gap_analysis_disabled_at_runtime",
            extra={"reason": f"{key_name} not set", "provider": provider},
        )
        return None

    if resume_text is None:
        log.warning("gap_analysis_disabled_at_runtime", extra={"reason": "resume document missing"})
        return None

    common = dict(
        model=model,
        resume_md=resume_text,
        timeout_seconds=cfg.gap_analysis.timeout_seconds,
        max_skills=cfg.gap_analysis.max_skills_per_job,
    )

    if provider == "anthropic":
        from anthropic import AsyncAnthropic
        return AnthropicGapAnalyzer(client=AsyncAnthropic(api_key=api_key), **common)

    if provider == "ollama":
        from ollama import AsyncClient
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        client = AsyncClient(host=cfg.relevance.ollama_host, headers=headers)
        return OllamaGapAnalyzer(client=client, **common)

    from google import genai  # type: ignore
    return GeminiGapAnalyzer(client=genai.Client(api_key=api_key), **common)


def _build_coach(cfg: AppConfig) -> CoachEngine | None:
    """Construct the coach engine if coach is enabled and the provider API key
    is present. Returns None (feature inert) otherwise — symmetric with
    _build_gap_analyzer. Provider/model fall back to the relevance settings
    when unset. Unlike the scorer/analyzer factories there are no file inputs
    here: profile/content come from the settings snapshot's documents at run
    time; a missing document degrades the coach snapshot."""
    if not cfg.coach.enabled:
        return None

    provider = cfg.coach.provider or cfg.relevance.provider
    model = cfg.coach.model or cfg.relevance.model

    if provider == "anthropic":
        api_key, key_name = cfg.secrets.anthropic_api_key, "anthropic_api_key"
    elif provider == "gemini":
        api_key, key_name = cfg.secrets.google_api_key, "google_api_key"
    elif provider == "ollama":
        api_key, key_name = cfg.secrets.ollama_api_key, "ollama_api_key"
    else:  # pragma: no cover — Pydantic Literal prevents this
        return None

    needs_key = not (provider == "ollama" and cfg.relevance.ollama_is_local)
    if needs_key and not api_key:
        log.warning(
            "coach_disabled_at_runtime",
            extra={"reason": f"{key_name} not set", "provider": provider},
        )
        return None

    common = dict(model=model, timeout_seconds=cfg.coach.timeout_seconds)

    if provider == "anthropic":
        from anthropic import AsyncAnthropic
        return AnthropicCoach(client=AsyncAnthropic(api_key=api_key), **common)

    if provider == "ollama":
        from ollama import AsyncClient
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        return OllamaCoach(client=AsyncClient(host=cfg.relevance.ollama_host, headers=headers), **common)

    from google import genai  # type: ignore
    return GeminiCoach(client=genai.Client(api_key=api_key), **common)


def _build_gap_digest(cfg: AppConfig, store: Any, *, days: int | None = None):
    """Compute the gap digest content + denominator. Shared by the digest tier
    and the --gaps-report CLI."""
    from datetime import datetime, timedelta, timezone
    window = days or cfg.gap_analysis.digest_window_days
    since = datetime.now(timezone.utc) - timedelta(days=window)
    gap_lists = store.recent_gap_lists(since)
    tally = tally_gaps(gap_lists)
    content = format_gap_digest(tally, window_days=window, total_jobs=len(gap_lists))
    return content, len(gap_lists), tally


async def _gaps_report(*, days: int | None = None) -> dict[str, Any]:
    stores = build_stores()
    snap = ConfigService(stores.settings).snapshot()
    cfg = snap.cfg if snap is not None else AppConfig()  # not set up: model defaults
    content, matches, tally = _build_gap_digest(cfg, stores.seen, days=days)
    return {
        "enabled": snap is not None and cfg.gap_analysis.enabled,
        "window_days": days or cfg.gap_analysis.digest_window_days,
        "matches": matches,
        "gaps": tally,
        "report": content,
    }


async def _run(
    tier: str, dry_run: bool = False, calibrate: bool = False, *,
    service: ConfigService | None = None,
) -> dict[str, Any]:
    stores = build_stores()
    service = service if service is not None else ConfigService(stores.settings)
    snap = service.snapshot()  # one snapshot for the whole cycle
    if snap is None:
        log.info("awaiting_setup", extra={"tier": tier})
        return {"tier": tier, "skipped": True, "reason": "not_configured"}
    cfg = snap.cfg
    discovered = stores.discovered
    health = stores.health

    if tier == "discovery":
        if not cfg.discovery.enabled:
            log.info("discovery_skipped", extra={"reason": "disabled in config"})
            return {"tier": "discovery", "skipped": True}
        # Active set = config slugs ∪ healthy discovered slugs (so we don't
        # re-validate slugs we already poll directly).
        active_set: set[tuple[str, str]] = set()
        for ats, slugs in [
            ("greenhouse", cfg.sources.greenhouse),
            ("lever", cfg.sources.lever),
            ("ashby", cfg.sources.ashby),
            ("workable", cfg.sources.workable),
            ("smartrecruiters", cfg.sources.smartrecruiters),
            ("rippling", cfg.sources.rippling),
            ("personio", cfg.sources.personio),
            ("recruitee", cfg.sources.recruitee),
            ("teamtailor", cfg.sources.teamtailor),
        ]:
            for slug in slugs:
                active_set.add((ats, slug))
        for row in discovered.list_healthy():
            active_set.add((row.ats_family, row.slug))

        yc_oss = make_yc_oss_fetcher(min_team_size=cfg.discovery.yc_oss_min_team_size)

        d_cfg = DiscoveryConfig(
            max_validations_per_run=cfg.discovery.max_validations_per_run,
            quarantine_after_failures=cfg.discovery.quarantine_after_failures,
            manual_companies=list(cfg.discovery.manual_companies),
            revalidate_after_days=cfg.discovery.revalidate_after_days,
            no_match_revalidate_after_days=cfg.discovery.no_match_revalidate_after_days,
            yc_oss_enabled=cfg.discovery.yc_oss_enabled,
            revalidate_reserve=cfg.discovery.revalidate_reserve,
            board_quarantine_after_failures=cfg.discovery.board_quarantine_after_failures,
        )

        async with httpx.AsyncClient() as client:
            await run_discovery(
                client=client,
                yc_oss=yc_oss,
                active_set=active_set,
                store=discovered,
                cfg=d_cfg,
                boards=stores.boards,
            )
            if cfg.discovery.board_discovery_enabled:
                try:
                    # Dedicated follow_redirects client: locate_careers_urls needs
                    # post-redirect final URLs, but the shared `client` above (no
                    # redirects) would make the sweep discover nothing.
                    async with httpx.AsyncClient(follow_redirects=True) as _bclient:
                        await run_board_discovery(
                            client=_bclient, seeds=_discovery_seeds(cfg.discovery.eu_seeds_enabled), boards=stores.boards,
                            max_sweeps_per_run=cfg.discovery.board_max_sweeps_per_run,
                            revalidate_after_days=cfg.discovery.board_revalidate_after_days,
                            quarantine_threshold=cfg.discovery.board_quarantine_after_failures,
                        )
                except Exception:  # noqa: BLE001 — a bad seed file / sweep must never skip recover_suppressed
                    log.exception("board_discovery_failed")
            if cfg.discovery.vc_firms:
                try:
                    # Same follow_redirects posture as the manual CLI
                    # (scripts/import_vc_portfolio.py): a16z redirects.
                    async with httpx.AsyncClient(follow_redirects=True) as _vclient:
                        await run_vc_discovery(
                            client=_vclient,
                            firms=list(cfg.discovery.vc_firms),
                            discovered=discovered,
                            boards=stores.boards,
                            source_state=stores.source_state,
                            active_set=active_set,
                            refresh_days=cfg.discovery.vc_refresh_days,
                            capture_cap=cfg.discovery.vc_capture_cap,
                            no_match_fresh_days=cfg.discovery.no_match_revalidate_after_days,
                        )
                except Exception:  # noqa: BLE001 — a broken portfolio fetch must never skip recover_suppressed
                    log.exception("vc_discovery_failed")
            await recover_suppressed(cfg=cfg, discovered=discovered, boards=stores.boards, health=health, client=client)
        return {"tier": "discovery"}

    if tier == "digest":
        if not cfg.gap_analysis.enabled:
            log.info("digest_skipped", extra={"reason": "gap_analysis disabled in config"})
            return {"tier": "digest", "skipped": True}
        if not cfg.secrets.discord_webhook_url:
            log.info("digest_skipped", extra={"reason": "discord_webhook_url not set"})
            return {"tier": "digest", "skipped": True}
        store = stores.seen
        content, matches, tally = _build_gap_digest(cfg, store)
        async with httpx.AsyncClient() as client:
            await send_gap_digest(client, cfg.secrets.discord_webhook_url, content)
        log.info("digest_sent", extra={"matches": matches, "skills": len(tally)})
        return {"tier": "digest", "matches": matches, "skills": len(tally)}

    # headless tier: same run_once pipeline, but connectors are driven by a
    # Playwright browser instead of the httpx client.
    browser_factory = None
    if tier == "headless":
        from src.headless import browser_session
        browser_factory = browser_session

    # ats / slow tiers fall through to the existing run_once pipeline
    store = stores.seen
    source_state = stores.source_state
    try:
        # Skip both permanently-suppressed (dead) and temporarily backed-off
        # (rate-limited) connectors this cycle. backoff_names auto-expires.
        suppressed = set(health.suppressed_names())
        suppressed |= health.backoff_names(int(time.time() * 1000))
    except Exception:  # noqa: BLE001 — degrade to polling everything if the table is unreadable
        log.warning("suppressed_names_unavailable")
        suppressed = frozenset()
    sightings = None
    if (
        tier == "slow"
        and (cfg.sources.hiringcafe.enabled or cfg.sources.adzuna.enabled)
        and cfg.discovery.hiringcafe_mining_enabled
        and discovered is not None
    ):
        sightings = []
    connectors = build_connectors(cfg, tier=tier, discovered=discovered, boards=stores.boards, suppressed=suppressed, sightings=sightings)  # type: ignore[arg-type]
    sinks = _build_sinks(cfg)
    relevance_scorer = _build_relevance_scorer(cfg, snap.documents.profile)
    gap_analyzer = _build_gap_analyzer(cfg, snap.documents.resume_text)
    dry_run = dry_run or calibrate  # calibrate never writes or notifies
    result = await run_once(
        cfg=cfg, tier=tier, store=store, source_state=source_state,  # type: ignore[arg-type]
        connectors=connectors, sinks=sinks,
        client_factory=lambda: httpx.AsyncClient(),
        dry_run=dry_run,
        relevance_scorer=relevance_scorer,
        gap_analyzer=gap_analyzer,
        health=health,
        calibrate=calibrate,
        rejected_store=stores.rejected if cfg.audit.enabled else None,
        browser_factory=browser_factory,
        max_concurrency=(3 if tier == "headless" else 40),
    )
    if sightings and not dry_run:
        try:
            from src.sightings import drain_sightings
            drain_sightings(
                sightings,
                discovered=discovered,
                boards=stores.boards,
                cap=cfg.discovery.candidate_capture_cap,
                no_match_fresh_days=cfg.discovery.no_match_revalidate_after_days,
            )
        except Exception:  # noqa: BLE001 — capture is best-effort; never break the cycle
            log.exception("sightings_drain_failed")
    # Local cycle telemetry for the /pipeline ops page. Skip dry-run/calibrate
    # cycles — they don't reflect real polling. Never let a telemetry write
    # break the cycle.
    if not dry_run:
        try:
            stores.events.record_cycle(
                tier=tier,
                fetched=result.fetched_count,
                matched=result.matched_count,
                notified=result.notified_count,
                duration_ms=result.duration_ms,
                failures=result.fetch_failures,
                llm_failures=result.llm_failures,
                new_count=result.new_count,
            )
        except Exception:  # noqa: BLE001 — telemetry is best-effort
            log.warning("pipeline_event_record_failed", extra={"tier": tier})
        # Ops alerts (separate channel; inert unless an ops sink is configured).
        # Best-effort by design.
        if cfg.secrets.ops_ntfy_topic_url or cfg.secrets.ops_discord_webhook_url:
            try:
                evaluator = OpsAlertEvaluator(
                    state=stores.alert_state,
                    events=stores.events,
                    thresholds=OpsThresholds(
                        llm_degraded_cycles=cfg.ops_notify.llm_degraded_cycles,
                        zero_yield_hours=cfg.ops_notify.zero_yield_hours,
                        cooldown_hours=cfg.ops_notify.cooldown_hours,
                        source_zero_yield_hours=cfg.ops_notify.source_zero_yield_hours,
                        source_min_baseline_rows=cfg.ops_notify.source_min_baseline_rows,
                    ),
                    rejected=stores.rejected,
                )
                alerts = evaluator.evaluate_cycle(
                    config_invalid_version_id=(
                        snap.degraded.invalid_version_id if snap.degraded is not None else None
                    ),
                )
                if alerts:
                    async with httpx.AsyncClient() as client:
                        for alert in alerts:
                            sent = await send_ops_alert(
                                client, alert,
                                ntfy_topic_url=cfg.secrets.ops_ntfy_topic_url,
                                discord_webhook_url=cfg.secrets.ops_discord_webhook_url,
                            )
                            if not sent and not alert.recovered:
                                # Every sink failed: re-arm so the next cycle retries
                                # instead of waiting out the cooldown. A lost recovery
                                # notice is accepted (re-arming would fake a firing state).
                                stores.alert_state.mark_recovered(alert.condition)
            except Exception:  # noqa: BLE001 — ops alerting must never break a cycle
                log.warning("ops_alert_eval_failed", extra={"tier": tier}, exc_info=True)
    # Dead-man's-switch heartbeat: ats-tier only (the fastest cadence), never
    # on dry-run/calibrate. External monitor alerts when pings stop.
    if tier == "ats" and not dry_run and cfg.secrets.heartbeat_url:
        await _ping_heartbeat(cfg.secrets.heartbeat_url)
    return {**asdict(result), "tier": tier}


def _cli() -> int:
    parser = argparse.ArgumentParser(prog="job-aggregator")
    parser.add_argument("--tier", choices=sorted(_VALID_TIERS), required=False)
    parser.add_argument("--dry-run", action="store_true", help="skip notify + state writes")
    parser.add_argument("--once", action="store_true", help="run a single full cycle (real send)")
    parser.add_argument(
        "--calibrate",
        action="store_true",
        help="score a sample without notifying; emit score distribution to re-tune score_low (implies --dry-run)",
    )
    parser.add_argument("--gaps-report", action="store_true", help="print aggregated resume skill gaps and exit (no notify)")
    parser.add_argument("--days", type=int, default=None, help="window in days for --gaps-report (default: config digest_window_days)")
    args = parser.parse_args()
    configure_logging()

    if args.gaps_report:
        report = asyncio.run(_gaps_report(days=args.days))
        if not report["enabled"]:
            sys.stderr.write("note: gap_analysis is disabled; showing any historical gaps.\n")
        json.dump(report, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
        return 0

    if not args.tier:
        parser.error("--tier is required unless --gaps-report is given")

    result = asyncio.run(_run(tier=args.tier, dry_run=args.dry_run, calibrate=args.calibrate))
    json.dump(result, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
