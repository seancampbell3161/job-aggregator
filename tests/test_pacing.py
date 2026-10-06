import httpx
import pytest

from src.pacing import (
    MAX_GAP, MIN_THROTTLED_GAP, VendorPacer, is_throttle, vendor_of,
)


class Clock:
    def __init__(self):
        self.t = 0.0
        self.slept = []

    def now(self):
        return self.t

    async def sleep(self, s):
        self.slept.append(s)
        self.t += s


def _err(code):
    req = httpx.Request("GET", "https://x")
    return httpx.HTTPStatusError("e", request=req, response=httpx.Response(code, request=req))


def test_gap_starts_at_zero_doubles_on_throttle_and_caps():
    p = VendorPacer(clock=lambda: 0.0, sleep=None)
    assert p.gap("greenhouse") == 0.0
    p.on_throttle("greenhouse")
    assert p.gap("greenhouse") == MIN_THROTTLED_GAP
    p.on_throttle("greenhouse")
    assert p.gap("greenhouse") == 1.0
    for _ in range(10):
        p.on_throttle("greenhouse")
    assert p.gap("greenhouse") == MAX_GAP


def test_success_decays_and_snaps_to_zero():
    p = VendorPacer(clock=lambda: 0.0, sleep=None)
    p.on_throttle("x")
    for _ in range(60):
        p.on_success("x")
    assert p.gap("x") == 0.0


@pytest.mark.asyncio
async def test_concurrent_waiters_are_spaced_by_gap():
    c = Clock()
    p = VendorPacer(clock=c.now, sleep=c.sleep)
    p.on_throttle("v")
    p.on_throttle("v")            # gap 1.0
    starts = []
    for _ in range(3):
        await p.wait("v")
        starts.append(c.t)
    assert starts == [0.0, 1.0, 2.0]


@pytest.mark.asyncio
async def test_wait_past_deadline_returns_false_without_reserving():
    c = Clock()
    p = VendorPacer(clock=c.now, sleep=c.sleep)
    for _ in range(4):
        p.on_throttle("v")        # gap 4.0
    assert await p.wait("v", deadline=10.0) is True     # slot at 0
    assert await p.wait("v", deadline=10.0) is True     # slot at 4
    assert await p.wait("v", deadline=10.0) is True     # slot at 8
    assert await p.wait("v", deadline=10.0) is False    # slot at 12 > 10: not reserved
    assert await p.wait("v", deadline=100.0) is True    # still slot 12, proving nothing was reserved
    assert c.t == 12.0


def test_vendor_of():
    assert vendor_of("greenhouse:stripe") == "greenhouse"
    assert vendor_of("workday:acme:External") == "workday"
    assert vendor_of("hiringcafe") == "hiringcafe"


def test_is_throttle():
    assert is_throttle("greenhouse", _err(429))
    assert is_throttle("oraclecloud", _err(400))
    assert is_throttle("workday", _err(400))
    assert not is_throttle("greenhouse", _err(400))
    assert not is_throttle("greenhouse", _err(503))
    assert not is_throttle("greenhouse", RuntimeError("x"))
