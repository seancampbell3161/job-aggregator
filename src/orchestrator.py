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
from src.notify.base import NotificationPayload, Sink, fanout
from src.notify.format import format_payload
from src.tailor.endpoint.auth import build_tailor_url
from src.gaps import GapAnalyzer, Gaps
from src.relevance import RelevanceScorer, Score
from src.pacing import PER_VENDOR_CONCURRENCY, is_throttle, vendor_of
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
    # can break failures down by type/connector.
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
    # What a dry run WOULD have sent. Empty on a real run, which notifies
    # instead. The wizard's preview renders these.
    would_notify: list[NotificationPayload] = field(default_factory=list)
    # Per successfully-fetched connector: how many of its postings are new to
    # this install. Feeds the adaptive cadence (quiet boards are polled less).
    polled: dict[str, int] = field(default_factory=dict)
    # Connectors still waiting on their vendor's pacer at the fetch deadline:
    # skipped this cycle, neither a failure nor a poll-health outcome.
    paced_out: list[str] = field(default_factory=list)
    # Connectors whose matches the scoring cap deferred to a later cycle.
    deferred_sources: list[str] = field(default_factory=list)


@dataclass
class _Tally:
    """Cycle-wide counters that only feed the filter_done log."""
    rejected: int = 0


def _screen_board(
    raws: list[RawPosting],
    *,
    cfg: AppConfig,
    store: SqliteSeenJobsStore,
    seen_in_run: set[str],
    result: RunResult,
    tally: _Tally,
    bypass: bool,
    dry_run: bool,
    calibrate: bool,
    rejected_store=None,
    evaluated_store=None,
    generation: int | None = None,
    app_version: str | None = None,
) -> tuple[list[tuple], int]:
    """Diff, normalize and filter one board's postings the moment its fetch
    completes. Returns the board's (posting, decision) matches and how many of
    its postings are new to this install. Rejections go to the audit trail
    here, in one transaction per board, and nothing else is kept, so a cycle's
    memory is bounded by its matches rather than by everything it fetched.

    bypass (calibrate / the wizard preview) screens every posting, seen or
    not, and neither skips nor writes the evaluation memory.

    Evaluation memory (evaluated_store + generation + app_version): an unseen
    posting already rejected under the current settings generation AND app
    version is not normalized again, so a settings change or an upgrade that
    changes normalize/filter logic re-judges it once. Rejected ids are recorded
    after filtering (never matches, never in a dry run or calibrate). `fresh`
    counts unseen postings with no memory row under ANY stamp, so neither a
    settings change nor an upgrade makes every board look active; it is
    computed the same way on the bypass path."""
    ids = [f"{r.source}:{r.external_id}" for r in raws]
    unseen = set(store.diff_new(ids))
    memory = evaluated_store is not None
    known = evaluated_store.known(unseen) if memory and unseen else {}
    fresh = len(unseen - known.keys())
    stamp = (generation, app_version)
    already_rejected = (
        {j for j, s in known.items() if s == stamp} if memory and not bypass else set()
    )
    matched: list[tuple] = []  # (NormalizedPosting, Decision)
    rejected: list[tuple] = []  # (NormalizedPosting, rejected_by)
    for raw, job_id in zip(raws, ids):
        if not bypass and job_id not in unseen:
            continue
        # Dedupe within the cycle by job_id (normalize derives the same id
        # from the same source:external_id): the same job can arrive from two
        # connectors (a mirror, or hiringcafe re-listing an ATS board).
        if job_id in seen_in_run:
            continue
        seen_in_run.add(job_id)
        # new_count keeps its pre-memory meaning: distinct postings this cycle
        # not in seen_jobs, counted before the memory skip below so a posting
        # it skips still counts (it is counted without being normalized).
        if job_id in unseen:
            result.new_count += 1
        if job_id in already_rejected:
            continue
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
        decision = evaluate(n, cfg.filters)
        if decision.allow:
            matched.append((n, decision))
        else:
            rejected.append((n, decision.rejected_by or "unknown"))
    tally.rejected += len(rejected)
    if rejected and rejected_store is not None and not dry_run and not calibrate:
        # Audit trail: record the first rejection of each posting with the
        # gate that dropped it. Best-effort — an audit-store failure must
        # never break the cycle.
        try:
            rejected_store.record_many(rejected)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "rejected_record_failed",
                extra={"source": rejected[0][0].source, "count": len(rejected),
                       "error": str(exc)},
            )
    if rejected and memory and not bypass and not dry_run and not calibrate:
        # Evaluation memory: best-effort too. Losing it only costs a
        # re-normalization next cycle.
        try:
            evaluated_store.record_many([n.job_id for n, _ in rejected],
                                        generation=generation, app_version=app_version)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "evaluated_record_failed",
                extra={"source": rejected[0][0].source, "count": len(rejected),
                       "error": str(exc)},
            )
    return matched, fresh


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
        # connectors polled every minute, adds up fast in the logs.
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


def _cap_for_scoring(matched: list, cap: int) -> tuple[list, list]:
    """Split matched (posting, decision) pairs into (scored this cycle,
    deferred). The scored slice is newest first; undated postings go last.
    Deferred pairs are dropped from this cycle: the seen-jobs store records
    nothing for them, and run_once drops their boards' cache hints, so they
    re-match next cycle."""
    if len(matched) <= cap:
        return matched, []
    ordered = sorted(
        matched,
        key=lambda pair: pair[0].posted_at.timestamp() if pair[0].posted_at else float("-inf"),
        reverse=True,
    )
    log.info("scoring_capped",
             extra={"matched": len(matched), "scored": cap, "deferred": len(matched) - cap})
    return ordered[:cap], ordered[cap:]


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
    ignore_seen: bool = False,
    evaluated_store=None,
    generation: int | None = None,
    app_version: str | None = None,
    pacer=None,
    fetch_deadline_s: float | None = None,
) -> RunResult:
    """Run one poll cycle over `connectors`, screening each board as it arrives.

    evaluated_store enables the evaluation memory, keyed on (generation,
    app_version), both required with it: unseen postings already rejected
    under this settings generation and app version are not normalized again,
    and this cycle's rejections are recorded (not in dry_run / calibrate;
    calibrate and ignore_seen bypass the skip). Matches are never recorded.
    Pass no evaluated_store to evaluate everything (the headless tier does).

    result.new_count counts the distinct postings fetched this cycle (deduped
    by job_id) that are not in seen_jobs, the meaning /pipeline and the
    zero-yield ops alert rely on. It is counted from the raw ids, so a posting
    the evaluation memory skips still counts although it is never normalized
    (a posting whose normalization fails counts too). result.polled[name]
    counts a board's unseen postings never evaluated in any generation; it
    drives the adaptive cadence."""
    if evaluated_store is not None and (generation is None or app_version is None):
        raise ValueError("evaluation memory needs both generation and app_version")
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

        new_states: dict[str, ConnectorState] = {}
        fetched_by: dict[str, str] = {}  # posting source -> connector name
        outcomes: list[tuple[str, str]] = []
        retry_after: dict[str, int | None] = {}
        seen_in_run: set[str] = set()
        tally = _Tally()
        # (connector index, that board's matches). Boards finish in any order;
        # matches are put back in connector order below.
        board_matches: list[tuple[int, list[tuple]]] = []

        def _absorb(i: int, name: str, res: FetchResult | Exception) -> None:
            """Book one board's fetch outcome and screen its postings. Plain
            synchronous code, so no other board interleaves with it."""
            outcome = classify_outcome(res)
            outcomes.append((name, outcome))
            if outcome == "rate_limited":
                # Managed via backoff (the connector is skipped for a cooldown,
                # then retried) — a soft, expected condition, not a hard cycle
                # failure. Don't add it to fetch_failures, so it doesn't flip the
                # cycle ok=0 / the heartbeat red. The per-fetch WARNING in
                # _fetch_one and the connector_health backoff row keep it visible.
                retry_after[name] = retry_after_seconds(res)
                return
            if isinstance(res, Exception):
                result.failed_sources.append(name)
                result.fetch_failures.append(
                    {"source": name, "error_type": type(res).__name__}
                )
                return
            result.fetched_count += len(res.postings)
            # A posting's source is not always its connector's name (hiringcafe
            # emits "hiringcafe:{family}:{slug}"); remember who fetched it.
            for raw in res.postings:
                fetched_by[raw.source] = name
            # calibrate and the wizard preview (ignore_seen) both bypass the
            # new-since-last-seen diff: calibrate needs a usable score
            # distribution from one run, and the preview must still show
            # matches on an instance the poller has already polled (otherwise
            # a preview run after the first real cycle shows nothing — exactly
            # when the user most wants reassurance).
            #
            # Per-board isolation: a screening failure (e.g. "database is
            # locked" while the hourly integrity check holds a read) is this
            # board's failure, not the cycle's. Raising here would cancel the
            # gather and lose every other board's matches, poll health, cadence
            # and telemetry. The board is left out of `polled`, so its schedule
            # is untouched and it stays due.
            try:
                board_matched, fresh = _screen_board(
                    res.postings, cfg=cfg, store=store, seen_in_run=seen_in_run,
                    result=result, tally=tally, bypass=(calibrate or ignore_seen),
                    dry_run=dry_run, calibrate=calibrate,
                    rejected_store=rejected_store, evaluated_store=evaluated_store,
                    generation=generation, app_version=app_version,
                )
            except Exception as exc:  # noqa: BLE001 — one board is not the cycle
                log.warning(
                    "board_screen_failed",
                    extra={"source": name, "error_type": type(exc).__name__,
                           "error": str(exc)[:200]},
                )
                result.failed_sources.append(name)
                result.fetch_failures.append(
                    {"source": name, "error_type": type(exc).__name__}
                )
                # No ETag saved: the next fetch is unconditional, so the
                # postings this cycle failed to screen come back.
                return
            if board_matched:
                board_matches.append((i, board_matched))
            result.polled[name] = fresh
            if res.new_state is not None:
                new_states[name] = res.new_state
                # Saved only after the board was screened: a hint saved for a
                # board whose screening then failed would make its next fetch
                # a 304, and its postings would wait until the board changed.
                # Saved now rather than at the end of the cycle so a later
                # notify failure doesn't lose it (state is independent of
                # seen-jobs marking).
                #
                # NOT in a dry run. These are per-connector ETag / cursor
                # hints: a dry run that advanced them would make the next REAL
                # cycle send If-None-Match, receive 304, and fetch nothing —
                # while the dry run marked nothing seen, so those postings
                # would never be alerted at all. A preview must leave no trace.
                if not dry_run:
                    try:
                        source_state.put(name, res.new_state)
                    except Exception as exc:  # noqa: BLE001 — a lost hint costs one full refetch
                        log.warning(
                            "source_state_put_failed",
                            extra={"source": name, "error_type": type(exc).__name__,
                                   "error": str(exc)[:200]},
                        )

        # Per-vendor caps live per cycle: asyncio primitives bind to the
        # cycle's event loop.
        vendor_sems: dict[str, asyncio.Semaphore] = {}
        deadline = (
            pacer.now() + fetch_deadline_s
            if pacer is not None and fetch_deadline_s is not None else None
        )

        async def _guarded(i: int, c: Connector) -> None:
            vendor = vendor_of(c.name)
            vsem = vendor_sems.setdefault(vendor, asyncio.Semaphore(PER_VENDOR_CONCURRENCY))
            # Vendor slot first, then the pacer: only the few boards holding a
            # vendor slot reserve pacer slots, so a throttle seen by one of
            # them slows the vendor's remaining boards within this cycle.
            async with vsem:
                if pacer is not None and not await pacer.wait(vendor, deadline=deadline):
                    # Not attempted this cycle: no outcome, no failure, no
                    # poll-health entry; it is picked up next cycle.
                    result.paced_out.append(c.name)
                    return
                async with sem:
                    name, res = await _fetch_one(c, client, prior[c.name], browser=browser)
                if pacer is not None:
                    if is_throttle(vendor, res):
                        pacer.on_throttle(vendor)
                    elif isinstance(res, FetchResult):
                        pacer.on_success(vendor)
                # Screen the board in the same step its fetch returns: nothing
                # can run in between, so its postings never wait in a queue
                # behind other boards. Peak memory is the boards in flight (at
                # most max_concurrency), the one being screened and the
                # matches, however many boards there are.
                # (Collecting results with as_completed doesn't give that
                # bound: boards that answer faster than they're screened pile
                # up in its done-queue.)
                _absorb(i, name, res)

        tasks = [asyncio.ensure_future(_guarded(i, c)) for i, c in enumerate(connectors)]
        try:
            await asyncio.gather(*tasks)
        finally:
            # Only has work to do if a board's task raised past _absorb's
            # isolation (or the cycle was cancelled): don't leave fetches
            # running against a client that is about to close.
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        # Poll-health circuit breaker (never in dry-run, which must not mutate
        # state). Runs for ats and slow — both poll real connectors that can
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

        if result.normalize_failures:
            log.error(
                "normalize_failures_in_cycle",
                extra={"count": len(result.normalize_failures)},
            )
        log.info(
            "diff_done",
            extra={"new": result.new_count, "total": result.fetched_count,
                   "paced_out": len(result.paced_out)},
        )

        # Back to connector order (what gathering every fetch used to give), so
        # scoring-cap ties, the calibration sample and notification order don't
        # depend on which board happened to answer first.
        board_matches.sort(key=lambda pair: pair[0])
        matched: list[tuple] = [m for _, ms in board_matches for m in ms]
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
                "rejected": tally.rejected,
            },
        )

        # Enrichment and scoring both scale with this list, so cap first.
        if relevance_scorer is not None and not calibrate:
            matched, deferred = _cap_for_scoring(matched, cfg.relevance.max_scored_per_cycle)
            # Same hazard as the dry-run note above: the ETag / Last-Modified
            # saved earlier this cycle would make the next fetch of a board
            # with deferred matches a 304 with zero postings, so those matches
            # would only come back when the board changed (and could age out
            # of filters.max_age_days first). Drop just those cache hints so
            # the next fetch is unconditional. The payload is connector-owned
            # bookkeeping (e.g. adzuna's metered call budget) and is kept.
            result.deferred_sources = sorted(
                {fetched_by.get(n.source, n.source) for n, _ in deferred})
            if not dry_run:
                for name in result.deferred_sources:
                    st = new_states.get(name, prior.get(name, ConnectorState()))
                    source_state.put(name, ConnectorState(payload=st.payload))

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
                    # histogram. dry_run/calibrate must not mutate state.
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
                result.would_notify.append(payload)
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
        # prevents two overlapping cycles from double-notifying the same
        # job_id (observed: 9 dupes in one 12h window when two ats-tier runs
        # raced on the same diff'd set).
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
            elif sinks:
                # All sinks failed; release the claim so a later cycle retries.
                store.release_claim(n.job_id)
            # No sinks configured is a valid setup, not a send failure: the
            # claim stays, so the match lands in the web UI and isn't re-scored
            # next cycle. Nothing was sent, so it doesn't count as notified.

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
