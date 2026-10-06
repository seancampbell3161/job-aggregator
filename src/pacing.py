"""Per-vendor request pacing shared by the poller and the starter-pack builder.

AIMD: the gap between request starts to one vendor doubles on a throttle
signal (429; HTTP 400 from Oracle Cloud and Workday, which use it to signal
throttling, proven by the 2026-10-06 pack build) and decays on a definite
answer. Slots are reserved before sleeping so concurrent waiters queue. Holds
only floats, so one pacer can outlive the event loop of a single cycle."""
from __future__ import annotations

import asyncio
import time

import httpx

MIN_THROTTLED_GAP = 0.5
MAX_GAP = 10.0
GAP_DECAY = 0.9
GAP_SNAP = 0.05
THROTTLE_400_FAMILIES = frozenset({"oraclecloud", "workday"})
PER_VENDOR_CONCURRENCY = 8


def vendor_of(connector_name: str) -> str:
    """A connector's vendor: its family prefix (greenhouse:stripe -> greenhouse)."""
    return connector_name.split(":", 1)[0]


def is_throttle(vendor: str, res: object) -> bool:
    if not isinstance(res, httpx.HTTPStatusError):
        return False
    code = res.response.status_code
    return code == 429 or (code == 400 and vendor in THROTTLE_400_FAMILIES)


class VendorPacer:
    def __init__(self, clock=time.monotonic, sleep=asyncio.sleep) -> None:
        self._clock = clock
        self._sleep = sleep
        self._gap: dict[str, float] = {}
        self._next: dict[str, float] = {}

    def gap(self, vendor: str) -> float:
        return self._gap.get(vendor, 0.0)

    async def wait(self, vendor: str, *, deadline: float | None = None) -> bool:
        now = self._clock()
        start = max(now, self._next.get(vendor, 0.0))
        if deadline is not None and start > deadline:
            return False
        self._next[vendor] = start + self.gap(vendor)
        if start > now:
            await self._sleep(start - now)
        return True

    def on_throttle(self, vendor: str) -> None:
        self._gap[vendor] = min(max(self.gap(vendor) * 2, MIN_THROTTLED_GAP), MAX_GAP)

    def on_success(self, vendor: str) -> None:
        gap = self.gap(vendor) * GAP_DECAY
        self._gap[vendor] = 0.0 if gap < GAP_SNAP else gap
