"""Ops-alert conditions over the pipeline_events telemetry.

A cooldown state machine per condition: fire once, stay quiet for
cooldown_hours while the condition persists, send a one-time recovery notice
when it clears. State persists in SQLite (ops_alert_state) so restarts don't
re-alert or drop a pending recovery."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from src.notify.ops import OpsAlert

_STALE_FLOOR_MINUTES = 15

# date.weekday(): Monday is 0, so Saturday/Sunday are 5/6.
_WEEKEND_DAYS = (5, 6)


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def business_lookback_hours(silence_hours: float, now: datetime) -> float:
    """Wall-clock hours to look back to cover ``silence_hours`` of Mon-Fri time.

    The per-source watchdog asks "has this family delivered nothing lately?",
    but most ATS families only publish on business days: recruitee is silent on
    8 of 12 weekend days and teamtailor on 7 of 12, against 0-1 for the
    high-volume families the thresholds were backtested on. Measuring silence in
    wall-clock hours therefore fires every single weekend for any bursty family
    that drifts over the qualifying bar — which is what recruitee and teamtailor
    did in August 2026, once discovery had grown them past 2000 baseline rows.

    Weekend days contribute nothing to the budget, so the window simply stretches
    across them: 24 business hours ending Sunday afternoon reaches back to Friday
    00:00, and the family's Friday volume keeps it quiet. A family that is
    genuinely dead on a Tuesday still trips at the usual 24 hours, because a
    midweek window never touches a weekend.

    Boundaries are UTC, matching the rest of the telemetry. These are
    European-heavy boards (CET/CEST), so UTC midnight is within an hour or two
    of local midnight — close enough for a threshold measured in days."""
    remaining = float(silence_hours)
    cursor = now
    while remaining > 0:
        day_start = cursor.replace(hour=0, minute=0, second=0, microsecond=0)
        if day_start >= cursor:
            day_start -= timedelta(days=1)
        # The segment [day_start, cursor) belongs to day_start's weekday --
        # classifying by cursor's would mis-label a cursor sitting exactly on
        # midnight as the day that is about to start.
        if day_start.weekday() not in _WEEKEND_DAYS:
            span = (cursor - day_start).total_seconds() / 3600.0
            if span >= remaining:
                cursor -= timedelta(hours=remaining)
                break
            remaining -= span
        cursor = day_start
    return (now - cursor).total_seconds() / 3600.0


@dataclass(frozen=True)
class OpsThresholds:
    llm_degraded_cycles: int = 2
    zero_yield_hours: int = 12
    cooldown_hours: int = 6
    # Per-source watchdog. Defaults were backtested against 45 days of real
    # history (2026-07-28): they produce exactly ONE alert over that span — the
    # 13-day hiring.cafe outage — and no false positives.
    #
    # source_baseline_days/source_min_baseline_rows/source_min_active_days
    # define which families are watched at all. The qualifying bar matters more
    # than the silence window: small bursty families (lever ~105/day, avature
    # ~72/day) legitimately go quiet for a day, and watching them produced 6
    # spurious alerts in the backtest. At 2000 rows/14d (~142/day) the watched
    # set is the eight high-volume families and nothing else.
    #
    # A newly added or newly restored source cannot alert until it has
    # source_min_active_days of history — a deliberate grace period.
    #
    # source_zero_yield_hours is BUSINESS hours, not wall-clock (see
    # business_lookback_hours). The backtest's clean result only held for
    # families that never go quiet; recruitee and teamtailor later drifted over
    # the bar as discovery grew them and alerted on every weekend, healthy.
    source_zero_yield_hours: int = 24
    source_baseline_days: int = 14
    source_min_baseline_rows: int = 2000
    source_min_active_days: int = 10


class OpsAlertEvaluator:
    def __init__(self, *, state, events, thresholds: OpsThresholds, rejected=None) -> None:
        self._state = state
        self._events = events
        self._t = thresholds
        # Optional: the per-source watchdog is skipped entirely when absent
        # (no rejected-postings store wired).
        self._rejected = rejected

    # -- state machine -------------------------------------------------------

    def _gate(self, condition: str, firing: bool, *, now_ms: int,
              title: str, body: str) -> OpsAlert | None:
        st = self._state.get(condition)
        if firing:
            cooldown_ms = self._t.cooldown_hours * 3_600_000
            if (st["active"] and st["last_sent_ms"] is not None
                    and now_ms - st["last_sent_ms"] < cooldown_ms):
                return None
            self._state.mark_active(condition, now_ms=now_ms)
            return OpsAlert(condition=condition, title=title, body=body)
        if st["active"]:
            self._state.mark_recovered(condition)
            return OpsAlert(
                condition=condition, title=f"recovered: {title}",
                body=f"The {condition} condition has cleared.", recovered=True,
            )
        return None

    # -- conditions ----------------------------------------------------------

    def _llm_degraded_firing(self, rows: list[dict]) -> bool:
        k = self._t.llm_degraded_cycles
        scored = [r for r in rows if r.get("tier") in ("ats", "slow")]
        if len(scored) < k:
            return False
        return all(r.get("llm_failures") for r in scored[-k:])

    def _zero_yield_firing(self, rows: list[dict], *, now_ms: int) -> bool:
        window_ms = self._t.zero_yield_hours * 3_600_000
        in_window = [
            r for r in rows
            if r["ts_ms"] >= now_ms - window_ms and r.get("new_count") is not None
        ]
        if not in_window:
            return False
        oldest = min(r["ts_ms"] for r in in_window)
        if now_ms - oldest < 0.9 * window_ms:
            return False  # telemetry doesn't cover the window yet (e.g. fresh restart)
        return all(r["new_count"] == 0 for r in in_window)

    def _watched_sources(self, *, now_ms: int) -> list[tuple[str, int, int, bool]]:
        """Watched families with their firing state.

        Returns (family, baseline_rows, baseline_active_days, is_silent) for
        EVERY watched family, not just the silent ones — a healthy family has to
        reach _gate with firing=False, or its condition would stay active
        forever after one alert and never emit a recovery notice.

        The pipeline-health guard is what makes this usable. A poller outage
        silences EVERY source at once: backtesting without the guard, the 55-hour
        outage ending 2026-07-23 produced nine simultaneous per-family alerts for
        a single incident. That case is pipeline_stopped's job, so this condition
        stays quiet unless some OTHER watched family is still delivering — which
        is exactly what distinguishes "one source broke" from "everything broke".

        The silence window is measured in business time (see
        business_lookback_hours), so a weekend stretches it rather than emptying
        it. Only the recent half moves: the baseline stays anchored to
        source_baseline_days of wall-clock history, so a longer weekend window
        shifts rows out of the baseline and can drop a marginal family below the
        qualifying bar. That direction is safe — it watches fewer families over
        a weekend, never more."""
        if self._rejected is None:
            return []
        get = getattr(self._rejected, "source_family_windows", None)
        if get is None:
            return []
        now = datetime.fromtimestamp(now_ms / 1000, timezone.utc)
        windows = get(
            baseline_days=self._t.source_baseline_days,
            silence_hours=business_lookback_hours(
                self._t.source_zero_yield_hours, now
            ),
        )
        watched = {
            f: (base, days, recent)
            for f, (base, days, recent) in windows.items()
            if base >= self._t.source_min_baseline_rows
            and days >= self._t.source_min_active_days
        }
        if not watched:
            return []
        if not any(recent > 0 for _, _, recent in watched.values()):
            return []  # nothing is flowing anywhere → whole-pipeline problem
        return sorted(
            ((f, base, days, recent == 0) for f, (base, days, recent) in watched.items()),
            key=lambda r: -r[1],
        )

    # -- entry points --------------------------------------------------------

    def evaluate_cycle(
        self, *, now_ms: int | None = None, config_invalid_version_id: int | None = None,
    ) -> list[OpsAlert]:
        """Poller-side conditions, evaluated after each recorded cycle.
        config_invalid_version_id is the snapshot's degraded version id (None
        when the newest settings version is valid)."""
        now = now_ms if now_ms is not None else _now_ms()
        days = max(1, (self._t.zero_yield_hours + 23) // 24)
        rows = self._events.recent_cycles(days, now_ms=now)
        out: list[OpsAlert] = []
        k = self._t.llm_degraded_cycles
        alert = self._gate(
            "llm_degraded", self._llm_degraded_firing(rows), now_ms=now,
            title="LLM scoring degraded",
            body=(f"The last {k} pipeline cycles all had LLM failures — postings "
                  "are flowing unscored (fail-open). See /pipeline."),
        )
        if alert:
            out.append(alert)
        alert = self._gate(
            "zero_yield", self._zero_yield_firing(rows, now_ms=now), now_ms=now,
            title="pipeline zero-yield",
            body=(f"Cycles are running but no new postings were seen in "
                  f"{self._t.zero_yield_hours}h — possible upstream/format "
                  "breakage. See /pipeline."),
        )
        if alert:
            out.append(alert)
        alert = self._gate(
            "config_fallback", config_invalid_version_id is not None, now_ms=now,
            title="settings fallback",
            body=(f"Settings version {config_invalid_version_id} failed validation, so the "
                  "app is running on the last valid version. Inspect with "
                  "`python -m src.settings status`, then import corrected settings or "
                  "`python -m src.settings restore ID`."),
        )
        if alert:
            out.append(alert)
        # Per-source watchdog. One condition key per family so each keeps its own
        # cooldown and emits its own recovery notice.
        hours = self._t.source_zero_yield_hours
        for family, base, days, silent in self._watched_sources(now_ms=now):
            alert = self._gate(
                f"source_zero_yield:{family}", silent, now_ms=now,
                title=f"source silent: {family}",
                body=(f"The {family} connectors delivered nothing in {hours}h of "
                      f"business time (weekends do not count toward the window), "
                      f"while other sources kept flowing. Baseline was {base} "
                      f"postings over {days} active days. Likely an upstream "
                      "block, a moved endpoint, or a schema change. See /pipeline."),
            )
            if alert:
                out.append(alert)
        return out

    def evaluate_tier_staleness(self, *, tier_intervals: dict[str, int],
                                now_ms: int | None = None) -> list[OpsAlert]:
        """One condition per tier: that tier has stopped recording cycles while
        the pipeline as a whole is still alive.

        pipeline_stopped can't cover this — it reads MAX(ts_ms) across ALL tiers,
        so any one healthy tier masks a dead one. The ats tier died twice that
        way (a corrupt index, then a null-title crash that killed the cycle
        before record_cycle()); both times the slow tier kept the overall
        heartbeat green and the only warning was an incidental per-source alert.

        Skipped entirely when nothing is fresh anywhere — that's a whole-poller
        outage, which is pipeline_stopped's job, and firing one alert per tier
        for it would just be noise (the same guard the source watchdog uses)."""
        now = now_ms if now_ms is not None else _now_ms()
        get = getattr(self._events, "last_cycle_per_tier", None)
        if get is None:
            return []
        rows = {r["tier"]: r["ts_ms"] for r in get() if r.get("ts_ms") is not None}
        watched = {t: i for t, i in tier_intervals.items() if t in rows}
        if not watched:
            return []  # no baseline yet — a fresh install must not alert

        def _threshold_min(interval: int) -> int:
            return max(3 * interval, _STALE_FLOOR_MINUTES)

        if not any(
            now - rows[t] <= _threshold_min(i) * 60_000 for t, i in watched.items()
        ):
            return []  # every tier is stale → whole-pipeline problem

        out: list[OpsAlert] = []
        for tier, interval in sorted(watched.items()):
            threshold = _threshold_min(interval)
            age_min = (now - rows[tier]) // 60_000
            alert = self._gate(
                f"tier_stopped:{tier}", (now - rows[tier]) > threshold * 60_000,
                now_ms=now,
                title=f"tier stalled: {tier}",
                body=(f"No {tier} cycle recorded in {age_min} min (expected every "
                      f"{interval} min), while other tiers kept running. The tier "
                      "is crashing before it records a cycle, or its job is wedged. "
                      "See /pipeline and the poller logs."),
            )
            if alert:
                out.append(alert)
        return out

    def evaluate_staleness(self, *, expected_interval_minutes: int,
                           now_ms: int | None = None) -> OpsAlert | None:
        """Watchdog-side condition: the poller has stopped writing cycles.
        Silent on an empty table — a fresh install has no liveness baseline."""
        now = now_ms if now_ms is not None else _now_ms()
        last = self._events.last_cycle_ms()
        threshold_min = max(3 * expected_interval_minutes, _STALE_FLOOR_MINUTES)
        firing = last is not None and (now - last) > threshold_min * 60_000
        return self._gate(
            "pipeline_stopped", firing, now_ms=now,
            title="pipeline stopped",
            body=(f"No pipeline cycle recorded in over {threshold_min} minutes — "
                  "the poller container is down or hung. See /pipeline."),
        )
