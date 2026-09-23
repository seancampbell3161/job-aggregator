from __future__ import annotations

import logging
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

from src.state import DiscoveredSlug
from src.state_sqlite import (
    SqliteConnectorHealthStore,
    SqliteDiscoveredSlugsStore,
    SqliteSeenJobsStore,
)
from src.web.context import config_ctx
from src.web.pipeline_activity import (
    CycleRow,
    LastCycle,
    PipelineActivity,
    StageFailures,
    Tally,
    TierStats,
    build_family_tallies,
    build_tier_heartbeats,
    compute_heartbeat,
    format_ago,
    merge_tier_last_cycles,
    tier_stale,
)

log = logging.getLogger(__name__)

_RECENT_CYCLES = 20   # rows in the /pipeline recent-cycles table (~3h of ats cadence)
_DIM_AFTER_MS = 24 * 3_600_000  # tallies older than this render dimmed


@dataclass(frozen=True)
class Liveness:
    """Is the poller alive? Epoch-ms timestamps, None when never recorded."""
    last_cycle_ms: int | None     # newest cycle of any tier, ok or not
    last_ats_ms: int | None       # newest ats-tier cycle (the cadence Home predicts)
    last_success_ms: int | None   # newest fully-ok cycle


@dataclass(frozen=True)
class HealthSummary:
    # Field names mirror the validation_status values SqliteDiscoveredSlugsStore
    # writes: "ok" | "failed" | "quarantined" | "no_match" (see src/state.py).
    ok: int
    failed: int
    quarantined: int
    no_match: int
    unhealthy: list[DiscoveredSlug] = field(default_factory=list)
    # Connectors auto-suppressed by the poll-health circuit breaker (dead 404/410).
    # Sourced from SqliteConnectorHealthStore, not discovered_slugs.
    suppressed: list[str] = field(default_factory=list)


def connector_health(rows: list[DiscoveredSlug]) -> HealthSummary:
    counts = {"ok": 0, "failed": 0, "quarantined": 0, "no_match": 0}
    for r in rows:
        if r.validation_status in counts:
            counts[r.validation_status] += 1
    # Only failed/quarantined are "unhealthy connectors". no_match rows are the
    # discovery negative-cache (slugs that matched no ATS — never polled) and can
    # number in the thousands, so they must not flood the unhealthy table.
    unhealthy = sorted(
        (r for r in rows if r.validation_status in ("failed", "quarantined")),
        key=lambda r: r.consecutive_failures,
        reverse=True,
    )
    return HealthSummary(
        ok=counts["ok"], failed=counts["failed"],
        quarantined=counts["quarantined"], no_match=counts["no_match"],
        unhealthy=unhealthy,
    )


@dataclass(frozen=True)
class ScoreAnalytics:
    histogram: list[int]          # index 0..10 → count of matches with that score
    total: int                    # notified matches only
    recent: int                   # notified matches with first_seen within window_days
    by_status: dict[str, int]     # notified matches only
    suppressed: int               # count of score-low suppressed postings (histogram-only)
    window_days: int


def score_analytics(
    matches: list[dict],
    suppressed: list[dict] | None = None,
    *,
    now: datetime,
    window_days: int = 7,
) -> ScoreAnalytics:
    """Build the pipeline score analytics. The histogram spans both notified
    `matches` and `suppressed` (score-low) postings so the full 0–10
    distribution is visible. total/recent/by_status are computed from notified
    matches only — suppressed postings are not part of the triage funnel."""
    suppressed = suppressed or []
    histogram = [0] * 11
    by_status: dict[str, int] = {}
    cutoff = (now - timedelta(days=window_days)).isoformat()
    recent = 0

    def _bump(score) -> None:
        # bool is a subclass of int — exclude it so a stray True/False can't land in the histogram
        if isinstance(score, int) and not isinstance(score, bool) and 0 <= score <= 10:
            histogram[score] += 1

    for r in matches:
        _bump(r.get("score"))
        status = r.get("status", "new")
        by_status[status] = by_status.get(status, 0) + 1
        if (r.get("first_seen") or "") >= cutoff:
            recent += 1
    for r in suppressed:
        _bump(r.get("score"))

    return ScoreAnalytics(
        histogram=histogram, total=len(matches), recent=recent,
        by_status=by_status, suppressed=len(suppressed), window_days=window_days,
    )


def aggregate_event_rows(rows: list[dict], *, window_days: int) -> PipelineActivity:
    """Aggregate per-cycle telemetry rows (one row per completed cycle, written
    by SqlitePipelineEventsStore) into PipelineActivity for _ops_cycles.html."""
    sums: dict[str, dict[str, float]] = defaultdict(
        lambda: {"cycles": 0, "fetched": 0, "matched": 0, "notified": 0, "duration_ms": 0}
    )
    by_type: Counter = Counter()
    by_conn: Counter = Counter()
    fam_types: dict[str, Counter] = defaultdict(Counter)
    last: LastCycle | None = None
    per_tier: dict[str, list[tuple[int, bool, bool]]] = defaultdict(list)
    llm_by_stage: dict[str, Counter] = defaultdict(Counter)
    llm_total = 0
    llm_degraded_cycles = 0
    last_type: dict[str, int] = {}
    last_conn: dict[str, int] = {}
    last_stage: dict[str, int] = {}

    for r in rows:
        tier = r.get("tier", "unknown")
        s = sums[tier]
        s["cycles"] += 1
        s["fetched"] += r.get("fetched", 0)
        s["matched"] += r.get("matched", 0)
        s["notified"] += r.get("notified", 0)
        s["duration_ms"] += r.get("duration_ms", 0)
        ts = r.get("ts_ms", 0)
        row_llm = r.get("llm_failures", [])
        row_ok = bool(r.get("ok", True))
        per_tier[tier].append((ts, row_ok, bool(row_llm)))
        # rows arrive ts-ascending; >= makes the most-recently-inserted row win
        # on a same-ms tie (back-to-back cycles), so the heartbeat reflects it.
        if last is None or ts >= last.ts_ms:
            last = LastCycle(ts_ms=ts, ok=row_ok, degraded=bool(row_llm))
        for f in r.get("failures", []):
            et = f.get("error_type", "unknown")
            src = f.get("source", "unknown")
            by_type[et] += 1
            by_conn[src] += 1
            fam_types[src.split(":", 1)[0]][et] += 1
            last_type[et] = max(last_type.get(et, 0), ts)
            last_conn[src] = max(last_conn.get(src, 0), ts)
        if row_llm:
            llm_degraded_cycles += 1
        for f in row_llm:
            stage = f.get("stage", "unknown")
            llm_by_stage[stage][f.get("error_type", "unknown")] += 1
            last_stage[stage] = max(last_stage.get(stage, 0), ts)
            llm_total += 1

    tiers = [
        TierStats(
            tier=t,
            cycles=int(s["cycles"]),
            avg_fetched=s["fetched"] / s["cycles"],
            avg_matched=s["matched"] / s["cycles"],
            avg_notified=s["notified"] / s["cycles"],
            avg_duration_ms=s["duration_ms"] / s["cycles"],
        )
        for t, s in sorted(sums.items())
    ]
    llm_failures_by_stage = [
        StageFailures(stage=stage, total=sum(c.values()), by_type=c.most_common(),
                      last_ms=last_stage.get(stage, 0))
        for stage, c in sorted(llm_by_stage.items())
    ]
    recent = [
        CycleRow(
            ts_ms=r.get("ts_ms", 0), tier=r.get("tier", "unknown"),
            fetched=r.get("fetched", 0), new_count=r.get("new_count"),
            matched=r.get("matched", 0), notified=r.get("notified", 0),
            duration_ms=r.get("duration_ms", 0), ok=bool(r.get("ok", True)),
            degraded=bool(r.get("llm_failures")),
            failures=[(f.get("source", "unknown"), f.get("error_type", "unknown"))
                      for f in r.get("failures", [])],
        )
        for r in rows[-_RECENT_CYCLES:][::-1]
    ]
    return PipelineActivity(
        window_days=window_days,
        tiers=tiers,
        failures_by_type=[Tally(n, c, last_type.get(n, 0)) for n, c in by_type.most_common()],
        failures_by_connector=[Tally(n, c, last_conn.get(n, 0)) for n, c in by_conn.most_common()],
        failures_by_family=build_family_tallies(by_conn, last_conn, fam_types),
        failures_total=sum(by_type.values()),
        last_cycle=last,
        llm_failures_total=llm_total,
        llm_failures_by_stage=llm_failures_by_stage,
        llm_degraded_cycles=llm_degraded_cycles,
        tier_heartbeats=build_tier_heartbeats(per_tier),
        recent=recent,
    )


class OpsProvider:
    """Composes the ops data sources behind fail-soft accessors. Each method
    returns its result, or None on any error (so a panel can render
    'unavailable' rather than 500-ing the page)."""

    def __init__(
        self,
        *,
        discovered: SqliteDiscoveredSlugsStore,
        seen: SqliteSeenJobsStore,
        window_days: int = 7,
        health: SqliteConnectorHealthStore | None = None,
        events=None,
    ) -> None:
        self._discovered = discovered
        self._seen = seen
        self._health = health
        self._events = events
        self._window_days = window_days

    def health(self) -> HealthSummary | None:
        try:
            summary = connector_health(self._discovered.list_all())
        except Exception as exc:  # noqa: BLE001 — panel degrades, page survives
            log.warning("ops_health_unavailable", extra={"error": str(exc)})
            return None
        suppressed: list[str] = []
        if self._health is not None:
            try:
                suppressed = sorted(self._health.suppressed_names())
            except Exception as exc:  # noqa: BLE001 — sub-section degrades, panel survives
                log.warning("ops_suppressed_unavailable", extra={"error": str(exc)})
        return replace(summary, suppressed=suppressed)

    def analytics(self, *, now: datetime | None = None) -> ScoreAnalytics | None:
        try:
            return score_analytics(
                self._seen.list_matches(),
                self._seen.list_suppressed(),
                now=now or datetime.now(timezone.utc),
                window_days=self._window_days,
            )
        except Exception as exc:  # noqa: BLE001 — panel degrades, page survives
            log.warning("ops_analytics_unavailable", extra={"error": str(exc)})
            return None

    def cycles(self, *, now_ms: int | None = None) -> PipelineActivity | None:
        """Aggregate the per-cycle telemetry the poller writes to SQLite; None
        when the events store isn't wired or can't be read."""
        if self._events is None:
            return None
        try:
            rows = self._events.recent_cycles(self._window_days, now_ms=now_ms)
        except Exception as exc:  # noqa: BLE001 — panel degrades, page survives
            log.warning("ops_cycles_unavailable", extra={"error": str(exc)})
            return None
        activity = aggregate_event_rows(rows, window_days=self._window_days)
        # Merge unwindowed per-tier anchors so a tier whose rows all aged
        # out of the window still surfaces (and reads stale) instead of
        # silently vanishing from the freshness strip.
        try:
            anchors = self._events.last_cycle_per_tier()
        except Exception as exc:  # noqa: BLE001 — anchor merge degrades, panel survives
            log.warning("ops_tier_anchors_unavailable", extra={"error": str(exc)})
            anchors = []
        if anchors:
            activity = replace(
                activity,
                tier_heartbeats=merge_tier_last_cycles(activity.tier_heartbeats, anchors),
            )
        return activity

    def last_success(self) -> int | None:
        """Epoch-ms of the most recent fully-successful cycle, for the /pipeline
        header. None when no cycle has succeeded or telemetry is unavailable."""
        if self._events is None:
            return None
        try:
            return self._events.last_success_ms()
        except Exception as exc:  # noqa: BLE001 — header stat degrades, page survives
            log.warning("ops_last_success_unavailable", extra={"error": str(exc)})
            return None

    def liveness(self) -> Liveness | None:
        """For Home's status line. None when telemetry is not wired or can't
        be read — distinct from Liveness(None, None, None), which means the
        poller has simply never recorded a cycle yet."""
        if self._events is None:
            return None
        try:
            tiers = {r["tier"]: r["ts_ms"] for r in self._events.last_cycle_per_tier()}
            return Liveness(
                last_cycle_ms=self._events.last_cycle_ms(),
                last_ats_ms=tiers.get("ats"),
                last_success_ms=self._events.last_success_ms(),
            )
        except Exception as exc:  # noqa: BLE001 — Home degrades, page survives
            log.warning("ops_liveness_unavailable", extra={"error": str(exc)})
            return None


def _ops_ctx(request: Request, **extra) -> dict:
    return {**config_ctx(request), **extra}


def register_ops_routes(app: FastAPI) -> None:
    @app.get("/pipeline", response_class=HTMLResponse)
    def pipeline(request: Request):
        ops = request.app.state.ops
        last_ms = ops.last_success()
        last_success_ago = format_ago(last_ms, int(time.time() * 1000)) if last_ms else None
        aud = getattr(request.app.state, "audit", None)
        tally = aud.tally() if (aud is not None and aud.available) else None
        return request.app.state.templates.TemplateResponse(
            request, "pipeline.html",
            _ops_ctx(
                request, health=ops.health(), analytics=ops.analytics(),
                last_success_ago=last_success_ago,
                rejected_7d=tally["total"] if tally else None,
            ),
        )

    @app.get("/pipeline/cycles", response_class=HTMLResponse)
    def pipeline_cycles(request: Request):
        ops = request.app.state.ops
        activity = ops.cycles()
        now_ms = int(time.time() * 1000)
        heartbeat = compute_heartbeat(activity, now_ms) if activity is not None else None
        # Per-tier freshness keyed by tier name, for the cycle-stats "last" column.
        tier_health = {}
        if activity is not None:
            for hb in activity.tier_heartbeats:
                tier_health[hb.tier] = {
                    "ago": format_ago(hb.last.ts_ms, now_ms),
                    "stale": tier_stale(hb, now_ms),
                    "ok": hb.last.ok,
                    "degraded": hb.last.degraded,
                }
        last_ms = ops.last_success()
        return request.app.state.templates.TemplateResponse(
            request, "_ops_cycles.html",
            _ops_ctx(
                request, activity=activity, heartbeat=heartbeat, tier_health=tier_health,
                now_ms=now_ms, dim_before_ms=now_ms - _DIM_AFTER_MS,
                last_success_ago=format_ago(last_ms, now_ms) if last_ms else None,
            ),
        )
