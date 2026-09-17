from __future__ import annotations

import asyncio
import logging
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

import httpx

from src.config import AppConfig
from src.connectors.base import Connector
from src.filters import evaluate
from src.models import ConnectorState, FetchResult, RawPosting, Tier
from src.normalize import normalize
from src.notify.base import Sink, fanout
from src.notify.format import format_payload
from src.tailor.endpoint.auth import build_tailor_url
from src.gaps import GapAnalyzer, Gaps
from src.relevance import RelevanceScorer, Score
from src.poll_health import classify_outcome, retry_after_seconds, update_poll_health
from src.state_sqlite import (
    SqliteConnectorHealthStore,
    SqliteSeenJobsStore,
    SqliteSourceStateStore,
)

log = logging.getLogger(__name__)

_CALIBRATION_SAMPLE_CAP = 200
_ENRICH_CONCURRENCY = 4
_MAX_ENRICH_PER_CYCLE = 200


@dataclass
class RunResult:
    fetched_count: int = 0
    new_count: int = 0
    matched_count: int = 0
    notified_count: int = 0
    failed_sources: list[str] = field(default_factory=list)
    # Per-failed-connector detail ({"source", "error_type"}), parallel to
    # failed_sources. Feeds the local SQLite cycle-telemetry sink so the ops page
    # can break failures down by type/connector the way CloudWatch does in AWS.
    fetch_failures: list[dict] = field(default_factory=list)
    # Per-failed LLM call ({"stage", "error_type"}). Parallel to fetch_failures;
    # feeds local cycle telemetry so /pipeline can break LLM degradation down by
    # stage/type. A single unreachable Ollama yields one relevance + one gap
    # entry per affected posting — two genuinely-failed calls.
    llm_failures: list[dict] = field(default_factory=list)
    # Per-skipped-posting detail ({"source", "error_type"}) for payloads that
    # blew up in normalize(). Deliberately NOT folded into fetch_failures: the
    # fetch itself succeeded, so flipping the source's poll-health would be wrong.
    normalize_failures: list[dict] = field(default_factory=list)
    duration_ms: int = 0


async def _fetch_one(
    conn: Connector,
    client: httpx.AsyncClient,
    state: ConnectorState,
    browser=None,
) -> tuple[str, FetchResult | Exception]:
    t0 = time.monotonic()
    try:
        if getattr(conn, "tier", None) == "headless":
            page = await browser.new_page()
            try:
                result = await conn.fetch(page, state)
            finally:
                await page.close()
                await page.context.close()
        else:
            result = await conn.fetch(client, state)
        log.info(
            "fetch_done",
            extra={
                "source": conn.name,
                "count": len(result.postings),
                "duration_ms": int((time.monotonic() - t0) * 1000),
                "status": "not_modified" if result.not_modified else "ok",
            },
        )
        return conn.name, result
    except Exception as exc:  # noqa: BLE001 — connector failures must not crash the cycle
        # Connector failures (PoolTimeout, HTTP errors, dead slugs) are expected
        # operational noise. Log a compact WARNING naming the exception instead of
        # a full stack trace — each traceback is ~3 KB and, across hundreds of
        # connectors polled every minute, dominated CloudWatch log-ingestion cost.
        log.warning(
            "fetch_failed",
            extra={
                "source": conn.name,
                "duration_ms": int((time.monotonic() - t0) * 1000),
                "status": "error",
                "error_type": type(exc).__name__,
                "error": str(exc)[:200],
            },
        )
        return conn.name, exc


async def _enrich_matched(
    matched: list[tuple], by_name: dict, client: httpx.AsyncClient, browser=None
) -> list[tuple]:
    """Enrich filter-survivors via their connector's enrich() (when supported),
    before scoring. Bounded to survivors + a per-cycle cap; fail-soft per item."""
    enrichable = [
        (i, n) for i, (n, _) in enumerate(matched)
        if getattr(by_name.get(n.source), "supports_enrich", False)
    ]
    if not enrichable:
        return matched
    if len(enrichable) > _MAX_ENRICH_PER_CYCLE:
        log.warning(
            "enrich_capped",
            extra={"enrichable": len(enrichable), "total_matched": len(matched), "cap": _MAX_ENRICH_PER_CYCLE},
        )
        enrichable = enrichable[:_MAX_ENRICH_PER_CYCLE]

    sem = asyncio.Semaphore(_ENRICH_CONCURRENCY)

    async def _one(i: int, n):
        async with sem:
            conn = by_name[n.source]
            try:
                if getattr(conn, "tier", None) == "headless":
                    page = await browser.new_page()
                    try:
                        return i, await conn.enrich(page, n)
                    finally:
                        await page.close()
                        await page.context.close()
                return i, await conn.enrich(client, n)
            except Exception as exc:  # noqa: BLE001 — enrich is best-effort
                log.warning("enrich_failed", extra={"source": n.source, "job_id": n.job_id, "error": str(exc)})
                return i, n

    out = list(matched)
    for i, new_n in await asyncio.gather(*(_one(i, n) for i, n in enrichable)):
        out[i] = (new_n, out[i][1])
    return out


async def run_once(
    *,
    cfg: AppConfig,
    tier: Tier,
    store: SqliteSeenJobsStore,
    source_state: SqliteSourceStateStore,
    connectors: Sequence[Connector],
    sinks: Sequence[Sink],
    client_factory: Callable[[], httpx.AsyncClient],
    browser_factory: Callable[[], Any] | None = None,
    dry_run: bool = False,
    relevance_scorer: RelevanceScorer | None = None,
    gap_analyzer: GapAnalyzer | None = None,
    health: SqliteConnectorHealthStore | None = None,
    calibrate: bool = False,
    max_concurrency: int = 40,
    rejected_store=None,
) -> RunResult:
    result = RunResult()
    t_start = time.monotonic()
    log.info(
        "invocation_start",
        extra={"tier": tier, "sources": [c.name for c in connectors], "dry_run": dry_run},
    )

    async with client_factory() as client, AsyncExitStack() as _stack:
        _has_headless = any(getattr(c, "tier", None) == "headless" for c in connectors)
        browser = await _stack.enter_async_context(browser_factory()) if (browser_factory and _has_headless) else None
        prior = source_state.get_many([c.name for c in connectors])
        # Bound concurrent fetches so the connector count (which grows over time)
        # can never exceed the HTTP connection pool and trigger httpx.PoolTimeout.
        # This decouples concurrency from the number of connectors.
        sem = asyncio.Semaphore(max_concurrency)

        async def _guarded(c: Connector) -> tuple[str, FetchResult | Exception]:
            async with sem:
                return await _fetch_one(c, client, prior[c.name], browser=browser)

        fetched = await asyncio.gather(*(_guarded(c) for c in connectors))

        all_postings: list[RawPosting] = []
        new_states: dict[str, ConnectorState] = {}
        outcomes: list[tuple[str, str]] = []
        retry_after: dict[str, int | None] = {}
        for name, res in fetched:
            outcome = classify_outcome(res)
            outcomes.append((name, outcome))
            if outcome == "rate_limited":
                # Managed via backoff (the connector is skipped for a cooldown,
                # then retried) — a soft, expected condition, not a hard cycle
                # failure. Don't add it to fetch_failures, so it doesn't flip the
                # cycle ok=0 / the heartbeat red. The per-fetch WARNING in
                # _fetch_one and the connector_health backoff row keep it visible.
                retry_after[name] = retry_after_seconds(res)
                continue
            if isinstance(res, Exception):
                result.failed_sources.append(name)
                result.fetch_failures.append(
                    {"source": name, "error_type": type(res).__name__}
                )
                continue
            all_postings.extend(res.postings)
            if res.new_state is not None:
                new_states[name] = res.new_state
        result.fetched_count = len(all_postings)

        # Poll-health circuit breaker (never in dry-run, which must not mutate
        # DynamoDB). Runs for ats and slow — both poll real connectors that can
        # 404/429. Permanent-dead (404/410) suppression stays ats-only (recovery
        # is the daily discovery re-probe, which rebuilds ats connectors); slow
        # gets only the self-expiring 429 backoff. tracked_names() is read once so
        # the 'ok' path is write-on-change.
        if health is not None and tier in ("ats", "slow") and not dry_run:
            update_poll_health(
                health, outcomes, health.tracked_names(),
                now_ms=int(time.time() * 1000),
                retry_after=retry_after,
                suppress_dead=(tier == "ats"),
            )

        # Persist new state immediately so a later notify failure doesn't lose
        # the cache hint (state is independent of seen-jobs marking).
        for name, st in new_states.items():
            source_state.put(name, st)

        # Normalize + dedup within invocation by job_id
        seen_in_run: set[str] = set()
        normalized = []
        for raw in all_postings:
            # Per-posting isolation. One malformed payload used to raise out of
            # run_once and kill the entire tier cycle: an Oracle requisition with
            # a null Title crashed every ats cycle for 12h (2026-08-02), and
            # because the cycle died before record_cycle(), the outage was
            # invisible to /pipeline and to the zero-yield alerts. Skipping the
            # single bad posting keeps the other ~30k flowing.
            try:
                n = normalize(
                    raw,
                    stack_keywords=cfg.filters.stack_any_of,
                    allowed_cities=cfg.filters.location.allowed_cities,
                )
            except Exception as exc:  # noqa: BLE001 — one bad posting is not a cycle failure
                result.normalize_failures.append(
                    {"source": raw.source, "error_type": type(exc).__name__}
                )
                log.warning(
                    "normalize_failed",
                    extra={"source": raw.source, "external_id": raw.external_id,
                           "error_type": type(exc).__name__},
                )
                continue
            if n.job_id in seen_in_run:
                continue
            seen_in_run.add(n.job_id)
            normalized.append(n)
        if result.normalize_failures:
            log.error(
                "normalize_failures_in_cycle",
                extra={"count": len(result.normalize_failures)},
            )

        # Diff against state
        new_ids = set(store.diff_new([n.job_id for n in normalized]))
        new_postings = [n for n in normalized if n.job_id in new_ids]
        result.new_count = len(new_postings)
        log.info("diff_done", extra={"new": result.new_count, "total": len(normalized)})

        # Filter. In calibration mode, filter the FULL normalized set (bypassing
        # the new-since-last-seen diff) so a single run yields a usable score
        # distribution rather than only the day's new arrivals.
        filter_source = normalized if calibrate else new_postings
        matched: list[tuple] = []  # (NormalizedPosting, Decision)
        for n in filter_source:
            decision = evaluate(n, cfg.filters)
            if decision.allow:
                matched.append((n, decision))
            elif rejected_store is not None and not dry_run and not calibrate:
                # Audit trail: record the first rejection of each posting with
                # the gate that dropped it. Best-effort — an audit-store failure
                # must never break the cycle.
                try:
                    rejected_store.record(n, rejected_by=decision.rejected_by or "unknown")
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "rejected_record_failed",
                        extra={"job_id": n.job_id, "error": str(exc)},
                    )
        # The calibration cap bounds the number of LLM scoring calls, so it must
        # apply to the MATCHED postings (post-filter). Capping the raw fetched
        # set first would usually yield zero matches — the keyword filter passes
        # only a small fraction of postings, so the first N fetched are typically
        # all rejected.
        if calibrate and len(matched) > _CALIBRATION_SAMPLE_CAP:
            log.info(
                "calibration_sample_capped",
                extra={"matched": len(matched), "cap": _CALIBRATION_SAMPLE_CAP},
            )
            matched = matched[:_CALIBRATION_SAMPLE_CAP]
        result.matched_count = len(matched)
        log.info(
            "filter_done",
            extra={
                "matched": result.matched_count,
                "rejected": len(filter_source) - result.matched_count,
            },
        )

        # Enrich filter-survivors with full detail (e.g. Workday's real JD)
        # before scoring, for connectors that support it. Bounded to survivors.
        matched = await _enrich_matched(matched, {c.name: c for c in connectors}, client, browser=browser)

        # Calibration mode: score every matched posting WITHOUT suppression and
        # emit a score distribution, then return. No notify, no DDB writes —
        # calibrate implies dry_run. Used to re-tune score_low for a new model.
        if calibrate:
            if relevance_scorer is None:
                log.warning("calibration_no_scorer", extra={"tier": tier})
                result.duration_ms = int((time.monotonic() - t_start) * 1000)
                return result
            histogram = {i: 0 for i in range(11)}
            fallbacks = 0
            for n, _decision in matched:
                score = await relevance_scorer.score(n)
                if score is None or score.value is None:
                    fallbacks += 1
                    continue
                histogram[score.value] += 1
                log.info(
                    "calibration_score",
                    extra={
                        "job_id": n.job_id,
                        "title": n.title,
                        "company": n.company,
                        "score": score.value,
                        "rationale": score.rationale,
                    },
                )
            would_suppress_at = {
                cutoff: sum(v for s, v in histogram.items() if s <= cutoff)
                for cutoff in (3, 4, 5)
            }
            log.info(
                "calibration_summary",
                extra={
                    "scored": sum(histogram.values()),
                    "fallbacks": fallbacks,
                    "histogram": histogram,
                    "would_suppress_at": would_suppress_at,
                },
            )
            result.duration_ms = int((time.monotonic() - t_start) * 1000)
            return result

        # Score relevance for matched postings (LLM call; fail-open).
        # Suppress notifications for postings the LLM scored at or below
        # `score_low` — fallbacks (score.value is None) and unscored runs
        # (relevance_scorer is None) always pass through.
        scored: list[tuple] = []  # (NormalizedPosting, Decision, Score | None, Gaps | None)
        suppressed_low = 0
        for n, decision in matched:
            score: Score | None = None
            if relevance_scorer is not None:
                score = await relevance_scorer.score(n)
                if score.is_fallback:
                    result.llm_failures.append(
                        {"stage": "relevance", "error_type": score.error_type or "unknown"}
                    )
            if (
                score is not None
                and score.value is not None
                and score.value <= cfg.relevance.score_low
            ):
                suppressed_low += 1
                if not dry_run:
                    # Record the suppressed posting so diff_new excludes it next
                    # cycle (no re-scoring) and the score feeds the pipeline
                    # histogram. dry_run/calibrate must not mutate DynamoDB.
                    store.mark_suppressed(
                        n.job_id, score=score.value,
                        rationale=score.rationale, posting=n,
                    )
                continue
            gaps: Gaps | None = None
            if gap_analyzer is not None:
                gaps = await gap_analyzer.analyze(n)
                if gaps.is_fallback:
                    result.llm_failures.append(
                        {"stage": "gap", "error_type": gaps.error_type or "unknown"}
                    )
            scored.append((n, decision, score, gaps))
        log.info(
            "score_done",
            extra={
                "scored": len(scored),
                "suppressed_low": suppressed_low,
                "fallbacks": sum(1 for _, _, s, _ in scored if s is not None and s.is_fallback),
            },
        )

        if dry_run:
            for n, decision, score, gaps in scored:
                payload = format_payload(
                    n, decision,
                    stack_filter=cfg.filters.stack_any_of,
                    score=score,
                    score_high=cfg.relevance.score_high,
                    score_low=cfg.relevance.score_low,
                    gaps=gaps.skills if gaps else None,
                )
                log.info("would_notify", extra={"job_id": n.job_id, "title": payload.title})
            log.info(
                "invocation_done",
                extra={
                    "duration_ms": int((time.monotonic() - t_start) * 1000),
                    "dry_run": True,
                },
            )
            result.duration_ms = int((time.monotonic() - t_start) * 1000)
            return result

        # Claim → notify → release-on-total-failure. The conditional claim
        # prevents two concurrent EventBridge-triggered invocations from
        # double-notifying the same job_id (observed: 9 dupes in one 12h window
        # when two ats-tier Lambdas raced on the same diff'd set).
        for n, decision, score, gaps in scored:
            gap_skills = gaps.skills if (gaps and gaps.skills) else None
            if not store.claim_for_notify(
                n.job_id,
                score=score.value if score else None,
                rationale=score.rationale if score else None,
                gaps=gap_skills,
                posting=n,
            ):
                log.info("notify_skipped_already_claimed", extra={"job_id": n.job_id})
                continue
            tailor_url = None
            if cfg.secrets.tailor_endpoint_url and cfg.secrets.tailor_signing_secret:
                tailor_url = build_tailor_url(
                    n.job_id,
                    endpoint_url=cfg.secrets.tailor_endpoint_url,
                    secret=cfg.secrets.tailor_signing_secret,
                )
            payload = format_payload(
                n, decision,
                stack_filter=cfg.filters.stack_any_of,
                score=score,
                score_high=cfg.relevance.score_high,
                score_low=cfg.relevance.score_low,
                gaps=gaps.skills if gaps else None,
                tailor_url=tailor_url,
            )
            results = await fanout(client, payload, sinks)
            any_ok = any(v is True for v in results.values())
            log.info(
                "notify_done",
                extra={
                    "job_id": n.job_id,
                    "results": {k: (True if v is True else str(v)) for k, v in results.items()},
                },
            )
            if any_ok:
                result.notified_count += 1
            else:
                # All sinks failed; release the claim so a later cycle retries.
                store.release_claim(n.job_id)

    result.duration_ms = int((time.monotonic() - t_start) * 1000)
    log.info(
        "invocation_done",
        extra={
            "tier": tier,
            "duration_ms": result.duration_ms,
            "fetched": result.fetched_count,
            "new": result.new_count,
            "matched": result.matched_count,
            "notified": result.notified_count,
            "failed_sources": result.failed_sources,
        },
    )
    return result
