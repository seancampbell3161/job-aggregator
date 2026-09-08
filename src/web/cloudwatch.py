from __future__ import annotations

import json
import statistics
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field

import boto3


@dataclass(frozen=True)
class TierStats:
    tier: str
    cycles: int
    avg_fetched: float
    avg_matched: float
    avg_notified: float
    avg_duration_ms: float


@dataclass(frozen=True)
class LastCycle:
    ts_ms: int
    ok: bool
    degraded: bool = False          # cycle had >=1 LLM fail-open failure


@dataclass(frozen=True)
class Tally:
    """One failure-tally entry plus the ts of its most recent occurrence, so
    the page can distinguish an active failure from a days-old burst."""
    name: str
    count: int
    last_ms: int


@dataclass(frozen=True)
class FamilyTally:
    """Fetch failures rolled up by connector family — the segment before the
    first ':' in a source name (``workday:target:targetcareers`` → ``workday``).
    `connectors` is how many distinct boards in the family failed; `top_type` is
    the family's dominant error type. Lets /pipeline show ~6 triage rows instead
    of a 200-connector wall, with the full per-connector list kept as detail."""
    family: str
    count: int
    connectors: int
    top_type: str
    last_ms: int


@dataclass(frozen=True)
class CycleRow:
    """One cycle in the /pipeline recent-cycles table (local telemetry only)."""
    ts_ms: int
    tier: str
    fetched: int
    new_count: int | None
    matched: int
    notified: int
    duration_ms: int
    ok: bool
    degraded: bool
    failures: list[tuple[str, str]]   # (source, error_type)


@dataclass(frozen=True)
class TierHeartbeat:
    """Freshness of a single tier (ats/slow/...). `median_gap_ms` is the typical
    interval between that tier's cycles in the window — used to decide staleness
    relative to the tier's own cadence, so no tier name needs hardcoding. 0 means
    fewer than two cycles in the window (cadence unknown) → the absolute
    _STALE_UNKNOWN_CADENCE_MS bound applies instead."""
    tier: str
    last: LastCycle
    median_gap_ms: int


@dataclass(frozen=True)
class Heartbeat:
    """Rendered overall pipeline heartbeat: a CSS class + a short label."""
    css: str                        # "ok" | "warn" | "bad" | "muted"
    label: str


@dataclass(frozen=True)
class StageFailures:
    stage: str                      # "relevance" | "gap"
    total: int
    by_type: list[tuple[str, int]]  # (error_type, count), most_common order
    last_ms: int = 0                # newest occurrence; 0 = unknown (CloudWatch path)


@dataclass(frozen=True)
class PipelineActivity:
    window_days: int
    tiers: list[TierStats]
    failures_by_type: list[Tally]
    failures_by_connector: list[Tally]
    failures_total: int
    last_cycle: LastCycle | None
    # LLM fail-open telemetry — populated only by the local (SQLite) path; the
    # AWS/CloudWatch path leaves these at defaults (see aggregate_log_events).
    llm_failures_total: int = 0
    llm_failures_by_stage: list[StageFailures] = field(default_factory=list)
    llm_degraded_cycles: int = 0
    # Same fetch failures as failures_by_connector, rolled up by connector family
    # (count-desc) for the /pipeline triage view. Defaulted so constructors that
    # predate it — and the AWS path before it populates them — still build.
    failures_by_family: list[FamilyTally] = field(default_factory=list)
    # Per-tier freshness, tier-sorted. Lets a dead fast tier (ats) be detected
    # even while a slow tier keeps the overall last_cycle looking recent.
    tier_heartbeats: list[TierHeartbeat] = field(default_factory=list)
    # Recent per-cycle rows for the detail table — local (SQLite) path only.
    recent: list[CycleRow] = field(default_factory=list)


def build_family_tallies(
    by_conn: Counter,
    last_conn: dict[str, int],
    fam_types: dict[str, Counter],
) -> list[FamilyTally]:
    """Roll per-connector failure tallies up to their connector family (the
    segment before the first ':'). Count and distinct-connector count come from
    `by_conn`; recency from `last_conn`; the dominant error type from `fam_types`
    (family → Counter of error types). Returned count-desc, family-tiebroken."""
    counts: Counter = Counter()
    conns: Counter = Counter()
    last: dict[str, int] = {}
    for name, cnt in by_conn.items():
        fam = name.split(":", 1)[0]
        counts[fam] += cnt
        conns[fam] += 1
        last[fam] = max(last.get(fam, 0), last_conn.get(name, 0))
    out = [
        FamilyTally(
            family=fam,
            count=cnt,
            connectors=conns[fam],
            top_type=(fam_types[fam].most_common(1)[0][0] if fam_types.get(fam) else "unknown"),
            last_ms=last.get(fam, 0),
        )
        for fam, cnt in counts.items()
    ]
    return sorted(out, key=lambda f: (-f.count, f.family))


def build_tier_heartbeats(
    per_tier_cycles: dict[str, list[tuple[int, bool, bool]]],
) -> list[TierHeartbeat]:
    """Reduce per-tier cycle samples to one heartbeat each. `per_tier_cycles`
    maps tier → list of (ts_ms, ok, degraded) in any order. The heartbeat's
    `last` is the newest sample; `median_gap_ms` is the median interval between
    consecutive cycles (0 if fewer than two)."""
    out: list[TierHeartbeat] = []
    for tier, cycles in sorted(per_tier_cycles.items()):
        ordered = sorted(cycles)  # by ts_ms ascending
        ts_list = [c[0] for c in ordered]
        last_ts, last_ok, last_deg = ordered[-1]
        gaps = [b - a for a, b in zip(ts_list, ts_list[1:])]
        median_gap = int(statistics.median(gaps)) if gaps else 0
        out.append(TierHeartbeat(
            tier=tier,
            last=LastCycle(ts_ms=last_ts, ok=last_ok, degraded=last_deg),
            median_gap_ms=median_gap,
        ))
    return out


def merge_tier_last_cycles(
    heartbeats: list[TierHeartbeat], last_per_tier: list[dict],
) -> list[TierHeartbeat]:
    """Fold unwindowed per-tier last-cycle anchors into windowed heartbeats.
    Tiers already in the window keep their heartbeat (its last cycle is the
    same newest row); tiers whose rows all aged out of the window are appended
    with unknown cadence, so the absolute staleness bound flags them — a dead
    tier stays visible forever instead of silently vanishing at 7 days."""
    known = {hb.tier for hb in heartbeats}
    out = list(heartbeats)
    for r in last_per_tier:
        if r["tier"] in known:
            continue
        out.append(TierHeartbeat(
            tier=r["tier"],
            last=LastCycle(ts_ms=r["ts_ms"], ok=r["ok"], degraded=r["degraded"]),
            median_gap_ms=0,
        ))
    return sorted(out, key=lambda hb: hb.tier)


# A tier is stale once it has missed several of its own cycles, but never before
# a small floor (so a one-off slow cycle on a low-cadence tier isn't flagged).
_STALE_GAP_MULTIPLIER = 5
_STALE_FLOOR_MS = 5 * 60_000

# With no in-window cadence to compare against, fall back to an absolute
# bound: 2× the slowest tier's cadence (discovery/digest are daily), so no
# legitimate tier can trip it but a dead one can't hide.
_STALE_UNKNOWN_CADENCE_MS = 48 * 3_600_000


def tier_stale(hb: TierHeartbeat, now_ms: int) -> bool:
    """True if a tier hasn't produced a cycle in well over its usual cadence.
    Unknown cadence (median_gap_ms == 0, i.e. <2 cycles in window) falls back
    to the absolute 48h bound so a long-dead tier still reads as stale."""
    if hb.median_gap_ms <= 0:
        return (now_ms - hb.last.ts_ms) > _STALE_UNKNOWN_CADENCE_MS
    threshold = max(_STALE_FLOOR_MS, _STALE_GAP_MULTIPLIER * hb.median_gap_ms)
    return (now_ms - hb.last.ts_ms) > threshold


def compute_heartbeat(activity: PipelineActivity | None, now_ms: int) -> Heartbeat:
    """The single overall heartbeat for the /pipeline header. A stalled tier
    (e.g. ats stopped while slow keeps ticking) takes priority and turns it red;
    otherwise it reflects the most-recent cycle's ok/degraded state as before."""
    heartbeats = activity.tier_heartbeats if activity else []
    stale = sorted(
        (hb for hb in heartbeats if tier_stale(hb, now_ms)),
        key=lambda hb: hb.last.ts_ms,  # oldest (most overdue) first
    )
    if stale:
        worst = stale[0]
        return Heartbeat("bad", f"{worst.tier} stalled · {format_ago(worst.last.ts_ms, now_ms)} ⚠")
    last = activity.last_cycle if activity else None
    if last is None:
        return Heartbeat("muted", "no cycles")
    ago = format_ago(last.ts_ms, now_ms)
    if not last.ok:
        return Heartbeat("bad", f"{ago} ⚠")
    if last.degraded:
        return Heartbeat("warn", f"{ago} ✓ LLM⚠")
    return Heartbeat("ok", f"{ago} ✓")


def aggregate_log_events(events: list[dict], *, window_days: int) -> PipelineActivity:
    """Pure aggregation of parsed log-event dicts into PipelineActivity.

    Each event dict is the structured log payload (``message`` is the event
    name, e.g. "invocation_done") plus a ``_ts_ms`` CloudWatch timestamp."""
    sums: dict[str, dict[str, float]] = defaultdict(
        lambda: {"cycles": 0, "fetched": 0, "matched": 0, "notified": 0, "duration_ms": 0}
    )
    by_type: Counter = Counter()
    by_conn: Counter = Counter()
    fam_types: dict[str, Counter] = defaultdict(Counter)
    last_type: dict[str, int] = {}
    last_conn: dict[str, int] = {}
    last: LastCycle | None = None
    per_tier: dict[str, list[tuple[int, bool, bool]]] = defaultdict(list)

    for e in events:
        kind = e.get("message")
        if kind == "invocation_done":
            tier = e.get("tier", "unknown")
            s = sums[tier]
            s["cycles"] += 1
            s["fetched"] += e.get("fetched", 0)
            s["matched"] += e.get("matched", 0)
            s["notified"] += e.get("notified", 0)
            s["duration_ms"] += e.get("duration_ms", 0)
            ts = e.get("_ts_ms", 0)
            ok = not e.get("failed_sources")
            per_tier[tier].append((ts, ok, False))
            if last is None or ts > last.ts_ms:
                last = LastCycle(ts_ms=ts, ok=ok)
        elif kind == "fetch_failed":
            ts = e.get("_ts_ms", 0)
            et = e.get("error_type", "unknown")
            src = e.get("source", "unknown")
            by_type[et] += 1
            by_conn[src] += 1
            fam_types[src.split(":", 1)[0]][et] += 1
            last_type[et] = max(last_type.get(et, 0), ts)
            last_conn[src] = max(last_conn.get(src, 0), ts)

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
    # NOTE: LLM fail-open failures are not parsed from CloudWatch here — that
    # telemetry is local-mode only. The llm_* fields fall to their defaults.
    return PipelineActivity(
        window_days=window_days,
        tiers=tiers,
        failures_by_type=[Tally(n, c, last_type.get(n, 0)) for n, c in by_type.most_common()],
        failures_by_connector=[Tally(n, c, last_conn.get(n, 0)) for n, c in by_conn.most_common()],
        failures_by_family=build_family_tallies(by_conn, last_conn, fam_types),
        failures_total=sum(by_type.values()),
        last_cycle=last,
        tier_heartbeats=build_tier_heartbeats(per_tier),
    )


def format_ago(ts_ms: int, now_ms: int) -> str:
    """Human 'time ago' for a millisecond epoch timestamp."""
    secs = max(0, (now_ms - ts_ms) // 1000)
    if secs < 45:
        return "just now"
    mins = secs // 60
    if mins < 60:
        return f"{max(1, mins)}m ago"
    hours = mins // 60
    if hours < 24:
        return f"{hours}h ago"
    return f"{hours // 24}d ago"


_FILTER_PATTERN = "?invocation_done ?fetch_failed"


def _parse_event(message: str, ts_ms: int) -> dict | None:
    """Extract the JSON payload from a log line (tolerant of any prefix the
    runtime prepends) and stamp the CloudWatch timestamp. None if unparseable."""
    start = message.find("{")
    if start == -1:
        return None
    try:
        payload = json.loads(message[start:])
    except (ValueError, TypeError):
        return None
    payload["_ts_ms"] = ts_ms
    return payload


def fetch_log_events(client, *, log_group: str, start_ms: int, end_ms: int) -> list[dict]:
    """Pull invocation_done + fetch_failed events in [start_ms, end_ms] and parse
    each into its structured dict (unparseable lines skipped). Paginates."""
    out: list[dict] = []
    token: str | None = None
    while True:
        kwargs = dict(
            logGroupName=log_group, startTime=start_ms, endTime=end_ms,
            filterPattern=_FILTER_PATTERN,
        )
        if token:
            kwargs["nextToken"] = token
        resp = client.filter_log_events(**kwargs)
        for ev in resp.get("events", []):
            parsed = _parse_event(ev.get("message", ""), ev.get("timestamp", 0))
            if parsed is not None:
                out.append(parsed)
        token = resp.get("nextToken")
        if not token:
            break
    return out


def load_pipeline_activity(
    *, log_group: str, region: str, window_days: int = 7, now_ms: int | None = None
) -> PipelineActivity:
    """Build a CloudWatch Logs client, pull the window's events, and aggregate.
    Raises on boto3 errors — the caller (OpsProvider) makes it fail-soft."""
    end = now_ms if now_ms is not None else int(time.time() * 1000)
    start = end - window_days * 86_400_000
    client = boto3.client("logs", region_name=region)
    events = fetch_log_events(client, log_group=log_group, start_ms=start, end_ms=end)
    return aggregate_log_events(events, window_days=window_days)
