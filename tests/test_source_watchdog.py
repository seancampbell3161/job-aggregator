"""Per-source zero-yield watchdog.

Thresholds here mirror the defaults, which were backtested against 45 days of
real history: exactly one alert over that span (the 13-day hiring.cafe outage),
no false positives. The cases below encode why each guard exists — every one of
them corresponds to something the backtest actually produced."""
from datetime import datetime, timedelta, timezone

import pytest

from src.ops_alerts import OpsAlertEvaluator, OpsThresholds, business_lookback_hours


class _FakeRejected:
    """Stands in for SqliteRejectedPostingsStore.source_family_windows."""

    def __init__(self, windows):
        self._windows = windows
        self.silence_hours = None

    def source_family_windows(self, *, baseline_days, silence_hours, now=None):
        self.silence_hours = silence_hours
        return self._windows


class _FakeState:
    def __init__(self):
        self.rows = {}

    def get(self, condition):
        return self.rows.get(condition, {"active": False, "last_sent_ms": None})

    def mark_active(self, condition, *, now_ms):
        self.rows[condition] = {"active": True, "last_sent_ms": now_ms}

    def mark_recovered(self, condition):
        self.rows[condition] = {"active": False, "last_sent_ms": None}


class _FakeEvents:
    def recent_cycles(self, days, *, now_ms=None):
        return []

    def last_cycle_ms(self):
        return None


def _evaluator(windows, state=None, rejected=None):
    return OpsAlertEvaluator(
        state=state or _FakeState(), events=_FakeEvents(),
        thresholds=OpsThresholds(), rejected=rejected or _FakeRejected(windows),
    )


def _ms(iso):
    return int(datetime.fromisoformat(iso).timestamp() * 1000)


# (baseline_rows, baseline_active_days, recent_rows)
HEALTHY = (40000, 14, 900)
SILENT = (17200, 13, 0)


def test_fires_when_one_watched_family_goes_silent():
    """The hiring.cafe shape: a high-volume family drops to zero while the rest
    of the pipeline keeps delivering."""
    alerts = _evaluator({"workday": HEALTHY, "hiringcafe": SILENT}).evaluate_cycle()
    conditions = [a.condition for a in alerts]
    assert "source_zero_yield:hiringcafe" in conditions
    assert "source_zero_yield:workday" not in conditions
    body = next(a for a in alerts if a.condition == "source_zero_yield:hiringcafe").body
    assert "17200" in body and "13 active days" in body


def test_stays_quiet_when_the_whole_pipeline_is_silent():
    """REGRESSION: backtesting without this guard, the 55-hour poller outage
    ending 2026-07-23 produced NINE simultaneous per-family alerts for a single
    incident. A total blackout is pipeline_stopped's job, not this one's."""
    alerts = _evaluator({
        "workday": (40000, 14, 0), "greenhouse": (13000, 14, 0),
        "ashby": (5000, 14, 0), "oraclecloud": (36000, 14, 0),
    }).evaluate_cycle()
    assert [a for a in alerts if a.condition.startswith("source_zero_yield")] == []


def test_low_volume_families_are_not_watched():
    """Small bursty sources legitimately go quiet for a day. Watching lever
    (~105/day) and avature (~72/day) produced 6 spurious backtest alerts."""
    alerts = _evaluator({
        "workday": HEALTHY,
        "lever": (1160, 12, 0),      # under the row bar
        "avature": (884, 14, 0),     # under the row bar
    }).evaluate_cycle()
    assert [a for a in alerts if a.condition.startswith("source_zero_yield")] == []


def test_new_source_cannot_alert_until_it_has_history():
    """A freshly added or freshly restored connector has volume but few active
    days — it must serve out a grace period rather than alert immediately."""
    alerts = _evaluator({
        "workday": HEALTHY,
        "hiringcafe": (5000, 2, 0),  # plenty of rows, only 2 days of history
    }).evaluate_cycle()
    assert [a for a in alerts if a.condition.startswith("source_zero_yield")] == []


def test_recovery_notice_is_emitted_when_a_source_comes_back():
    """A healthy family must still reach _gate, or its condition stays active
    forever after one alert and no recovery is ever sent."""
    state = _FakeState()
    first = _evaluator({"workday": HEALTHY, "hiringcafe": SILENT}, state).evaluate_cycle()
    assert any(a.condition == "source_zero_yield:hiringcafe" for a in first)

    back = _evaluator({"workday": HEALTHY, "hiringcafe": (17200, 13, 640)}, state).evaluate_cycle()
    recovery = [a for a in back if a.condition == "source_zero_yield:hiringcafe"]
    assert len(recovery) == 1 and recovery[0].recovered is True


def test_repeat_alert_is_suppressed_by_cooldown():
    state = _FakeState()
    windows = {"workday": HEALTHY, "hiringcafe": SILENT}
    _evaluator(windows, state).evaluate_cycle()
    again = _evaluator(windows, state).evaluate_cycle()
    assert [a for a in again if a.condition == "source_zero_yield:hiringcafe"] == []


def test_watchdog_is_inert_without_a_rejected_store():
    """No rejected-postings store wired."""
    ev = OpsAlertEvaluator(state=_FakeState(), events=_FakeEvents(),
                           thresholds=OpsThresholds(), rejected=None)
    assert [a for a in ev.evaluate_cycle() if a.condition.startswith("source_zero_yield")] == []


def test_each_family_gets_an_independent_condition_key():
    """Two simultaneous failures must not collapse into one alert, and one
    family's cooldown must not mute another's."""
    alerts = _evaluator({
        "workday": HEALTHY, "hiringcafe": SILENT, "adzuna": (8300, 10, 0),
    }).evaluate_cycle()
    keys = {a.condition for a in alerts if a.condition.startswith("source_zero_yield")}
    assert keys == {"source_zero_yield:hiringcafe", "source_zero_yield:adzuna"}


@pytest.mark.parametrize("rows,days,should_fire", [
    (2000, 10, True),    # exactly at both bars
    (1999, 10, False),   # one row under
    (2000, 9, False),    # one day under
])
def test_qualifying_bars_are_inclusive(rows, days, should_fire):
    alerts = _evaluator({"workday": HEALTHY, "x": (rows, days, 0)}).evaluate_cycle()
    fired = any(a.condition == "source_zero_yield:x" for a in alerts)
    assert fired is should_fire


# -- weekend-aware silence window --------------------------------------------
#
# REGRESSION (2026-08-23): recruitee and teamtailor each fired every ~6h through
# a weekend. Both connectors were healthy — recruitee served 2,942 HTTP 200s and
# 137k postings in 60h, teamtailor 4,541 HTTP 304s — they simply publish nothing
# on Sat/Sun, and discovery had just grown them past the qualifying bar that the
# 2026-07-28 backtest calibrated on families which never go quiet.


@pytest.mark.parametrize("now_iso,expected,why", [
    ("2026-08-19T15:43:00+00:00", 24.0, "midweek is untouched: Wed back to Tue"),
    ("2026-08-23T15:43:00+00:00", 63.72, "Sun back to Fri 00:00, skipping Sat+Sun"),
    ("2026-08-24T15:43:00+00:00", 72.0, "Mon back to Fri 15:43"),
    ("2026-08-22T15:43:00+00:00", 39.72, "Sat back to Fri 00:00"),
    ("2026-08-21T15:43:00+00:00", 24.0, "Fri back to Thu, no weekend crossed"),
    ("2026-08-22T00:00:00+00:00", 24.0, "midnight entering Sat still measures Fri"),
])
def test_business_lookback_stretches_across_weekends(now_iso, expected, why):
    got = business_lookback_hours(24, datetime.fromisoformat(now_iso))
    assert got == pytest.approx(expected, abs=0.01), why


def test_business_lookback_never_shortens_the_window():
    """The window may stretch but must never dip below the configured budget,
    or a family would be judged on less silence than the threshold asks for."""
    start = datetime(2026, 8, 17, tzinfo=timezone.utc)
    for hour in range(24 * 14):
        assert business_lookback_hours(24, start + timedelta(hours=hour)) >= 24.0


def test_weekend_evaluation_asks_for_a_stretched_window():
    """The evaluator must hand the widened span to the store — the fix is inert
    if the business-time maths never reaches the query."""
    rejected = _FakeRejected({"workday": HEALTHY})
    _evaluator(None, rejected=rejected).evaluate_cycle(
        now_ms=_ms("2026-08-23T15:43:00+00:00")
    )
    assert rejected.silence_hours == pytest.approx(63.72, abs=0.01)

    midweek = _FakeRejected({"workday": HEALTHY})
    _evaluator(None, rejected=midweek).evaluate_cycle(
        now_ms=_ms("2026-08-19T15:43:00+00:00")
    )
    assert midweek.silence_hours == pytest.approx(24.0, abs=0.01)


def test_family_quiet_only_over_the_weekend_does_not_alert():
    """recruitee's actual shape on 2026-08-23: nothing since Friday, but Friday
    itself delivered 1043. The stretched window sees that Friday volume."""
    alerts = _evaluator({
        "workday": HEALTHY,
        "recruitee": (2293, 11, 1043),   # recent window reaches back into Friday
    }).evaluate_cycle(now_ms=_ms("2026-08-23T15:43:00+00:00"))
    assert [a for a in alerts if a.condition.startswith("source_zero_yield")] == []


def test_family_dead_since_before_the_weekend_still_alerts():
    """The weekend must not become a blanket amnesty: a family that delivered
    nothing on Friday either is genuinely broken and has to fire."""
    alerts = _evaluator({
        "workday": HEALTHY,
        "recruitee": (2293, 11, 0),
    }).evaluate_cycle(now_ms=_ms("2026-08-23T15:43:00+00:00"))
    assert "source_zero_yield:recruitee" in [a.condition for a in alerts]
