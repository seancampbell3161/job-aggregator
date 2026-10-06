"""Adaptive per-board poll cadence: a board that yields new postings is
polled every cycle; a quiet one backs off (doubling) to at most an hour, or
to its tier interval when that is already longer."""
MAX_INTERVAL_S = 3600
DUE_SLACK_MS = 30_000   # land on a cycle tick, not just after it


def next_interval(prev_s: int | None, *, fresh: int, forced: bool, base_s: int) -> int:
    if fresh > 0 or forced:
        return base_s
    return max(base_s, min((prev_s or base_s) * 2, MAX_INTERVAL_S))
