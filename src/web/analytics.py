from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

from src.digest import tally_gaps
from src.state_sqlite import SqliteSeenJobsStore
from src.web.funnel import Funnel, Pipeline, Rate, build_funnel, build_pipeline, pipeline_rates

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class WeekBucket:
    label: str   # ISO date (YYYY-MM-DD) of the week's Monday
    count: int


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _parse_day(first_seen: str) -> date | None:
    try:
        return datetime.fromisoformat(first_seen).date()
    except (TypeError, ValueError):
        return None


def matches_by_week(matches: list[dict], *, now: datetime, max_weeks: int = 12) -> list[WeekBucket]:
    """Bucket notified matches by ISO week (Monday) of first_seen. Emits weeks
    from the earliest match's week through the current week, clamped to the
    trailing ``max_weeks``. Empty/unparseable input contributes nothing; no
    matches at all -> []."""
    days = [d for d in (_parse_day(m.get("first_seen", "")) for m in matches) if d is not None]
    if not days:
        return []
    counts: Counter = Counter(_monday(d) for d in days)
    current = _monday(now.date())
    start = max(min(counts), current - timedelta(weeks=max_weeks - 1))
    out: list[WeekBucket] = []
    wk = start
    while wk <= current:
        out.append(WeekBucket(label=wk.isoformat(), count=counts[wk]))
        wk += timedelta(days=7)
    return out


def _rank(values: list[str], top: int) -> list[tuple[str, int]]:
    counts: Counter = Counter(v for v in values if v)
    # frequency desc, ties broken by name asc (stable, readable ordering)
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:top]


def rank_by_company(matches: list[dict], *, top: int = 10) -> list[tuple[str, int]]:
    """Rank companies by how many notified matches each produced."""
    return _rank([m.get("company", "") for m in matches], top)


def rank_by_ats(matches: list[dict], *, top: int = 10) -> list[tuple[str, int]]:
    """Rank ATS providers — the prefix of `source` before ':' (e.g.
    'greenhouse:stripe' -> 'greenhouse'); a source with no ':' is its own key."""
    return _rank([(m.get("source", "") or "").split(":", 1)[0] for m in matches], top)


def gap_window(matches: list[dict], *, now: datetime, window_days: int = 30) -> tuple[list[tuple[str, int]], int]:
    """Filter matches to first_seen >= now - window_days, then return the gap
    tally (singletons kept, via min_count=1) and the denominator — the number of
    notified matches in the window (for the 'X of last N matches' line).

    first_seen is an ISO-8601 UTC string, so a lexicographic >= against
    (now - window).isoformat() is a correct time comparison."""
    cutoff = (now - timedelta(days=window_days)).isoformat()
    windowed = [m for m in matches if (m.get("first_seen") or "") >= cutoff]
    tally = tally_gaps([list(m.get("gaps") or []) for m in windowed], min_count=1)
    return tally, len(windowed)


@dataclass(frozen=True)
class MatchAnalyticsSummary:
    total: int
    by_week: list[WeekBucket]
    by_company: list[tuple[str, int]]
    by_ats: list[tuple[str, int]]
    gaps: list[tuple[str, int]]
    gaps_total: int          # denominator: notified matches within the gaps window
    window_days: int
    funnel: Funnel           # raw node/link graph; the panel renders `pipeline`
    pipeline: Pipeline       # triage split, progression steps, current standing, flows
    rates: list[Rate]


class MatchAnalytics:
    """Fail-soft analytics over notified matches. One list_matches() scan feeds
    every section; any error degrades the whole page to 'unavailable' (mirrors
    OpsProvider)."""

    def __init__(self, *, seen: SqliteSeenJobsStore, window_days: int = 30, max_weeks: int = 12) -> None:
        self._seen = seen
        self._window_days = window_days
        self._max_weeks = max_weeks

    def summary(self, *, now: datetime | None = None) -> MatchAnalyticsSummary | None:
        try:
            matches = self._seen.list_matches()
        except Exception as exc:  # noqa: BLE001 — page degrades, never 500s
            log.warning("match_analytics_unavailable", extra={"error": str(exc)})
            return None
        now = now or datetime.now(timezone.utc)
        gaps, gaps_total = gap_window(matches, now=now, window_days=self._window_days)
        funnel = build_funnel(matches)
        return MatchAnalyticsSummary(
            total=len(matches),
            by_week=matches_by_week(matches, now=now, max_weeks=self._max_weeks),
            by_company=rank_by_company(matches),
            by_ats=rank_by_ats(matches),
            gaps=gaps, gaps_total=gaps_total, window_days=self._window_days,
            funnel=funnel, pipeline=build_pipeline(matches), rates=pipeline_rates(matches),
        )


def register_analytics_routes(app: FastAPI) -> None:
    @app.get("/analytics", response_class=HTMLResponse)
    def analytics(request: Request):
        summary = request.app.state.match_analytics.summary()
        return request.app.state.templates.TemplateResponse(
            request, "analytics.html", {"summary": summary},
        )
