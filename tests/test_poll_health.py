import httpx
import pytest

from src.models import FetchResult
from src.poll_health import (
    DEAD_AFTER_CYCLES, DEFAULT_BACKOFF_SECONDS, classify_outcome,
    retry_after_seconds, update_poll_health,
)
from src.sqlite_db import connect
from src.state_sqlite import SqliteConnectorHealthStore


def _http_error(status: int) -> httpx.HTTPStatusError:
    req = httpx.Request("GET", "https://x")
    resp = httpx.Response(status, request=req)
    return httpx.HTTPStatusError("boom", request=req, response=resp)


def test_classify_ok_for_fetchresult():
    res = FetchResult(postings=[], new_state=None, not_modified=False)
    assert classify_outcome(res) == "ok"


def test_classify_dead_for_404_and_410():
    assert classify_outcome(_http_error(404)) == "dead"
    assert classify_outcome(_http_error(410)) == "dead"


def test_classify_transient_for_5xx_timeout_and_other():
    assert classify_outcome(_http_error(503)) == "transient"
    assert classify_outcome(httpx.PoolTimeout("pool")) == "transient"
    assert classify_outcome(RuntimeError("weird")) == "transient"


def test_classify_rate_limited_for_429():
    assert classify_outcome(_http_error(429)) == "rate_limited"


def test_classify_blocked_for_400_401_and_403():
    """A bot challenge / WAF rule / revoked key is not 'transient': it does not
    clear on its own, so it must not silently no-op the way a timeout does.

    400 counts too. Workday answers 400 rather than 403 to a client it will not
    serve, so without this a board that permanently refuses us would land in
    'transient' (a no-op) and never show up in connector_health — the operator
    would see only a slow decline to zero yield, with the 400 indistinguishable
    from 5xx noise in the dashboard's by-error-type tally."""
    assert classify_outcome(_http_error(400)) == "blocked"
    assert classify_outcome(_http_error(401)) == "blocked"
    assert classify_outcome(_http_error(403)) == "blocked"


def test_classify_400_does_not_auto_suppress_the_connector():
    """Filing 400 as 'blocked' must stay cheap when it really was a malformed
    request of ours: 'blocked' records a streak and warns, but never suppresses
    (a refusal can be lifted as easily as it was applied)."""
    from src.poll_health import DEAD_AFTER_CYCLES, update_poll_health

    class _Health:
        def __init__(self): self.dead = 0; self.suppressed = []
        def tracked_names(self): return set()
        def clear(self, name): pass
        def record_dead(self, name): self.dead += 1; return self.dead
        def mark_suppressed(self, name): self.suppressed.append(name)
        def mark_backoff(self, name, until_ms): pass

    h = _Health()
    for _ in range(DEAD_AFTER_CYCLES + 2):
        update_poll_health(h, [("workday:acme", "blocked")], set(), suppress_dead=True)
    assert h.dead == DEAD_AFTER_CYCLES + 2   # streak recorded
    assert h.suppressed == []                # but never auto-suppressed


def test_retry_after_seconds_parses_integer_and_handles_missing():
    req = httpx.Request("GET", "https://x")
    with_header = httpx.HTTPStatusError(
        "rl", request=req, response=httpx.Response(429, headers={"Retry-After": "120"}, request=req))
    assert retry_after_seconds(with_header) == 120
    no_header = httpx.HTTPStatusError(
        "rl", request=req, response=httpx.Response(429, request=req))
    assert retry_after_seconds(no_header) is None
    assert retry_after_seconds(httpx.PoolTimeout("x")) is None  # not an HTTPStatusError


def test_update_rate_limited_marks_self_expiring_backoff(health):
    now = 1_000_000
    update_poll_health(
        health, [("workable:blaze", "rate_limited")], health.tracked_names(),
        now_ms=now, retry_after={"workable:blaze": 300},
    )
    assert "workable:blaze" in health.backoff_names(now)               # backed off now
    assert "workable:blaze" in health.backoff_names(now + 299_000)     # within window
    assert "workable:blaze" not in health.backoff_names(now + 301_000)  # expired
    assert health.suppressed_names() == set()                          # not the dead path


def test_update_rate_limited_default_backoff_when_no_retry_after(health):
    now = 1_000_000
    update_poll_health(health, [("x:y", "rate_limited")], health.tracked_names(), now_ms=now)
    assert "x:y" in health.backoff_names(now + DEFAULT_BACKOFF_SECONDS * 1000 - 1000)
    assert "x:y" not in health.backoff_names(now + DEFAULT_BACKOFF_SECONDS * 1000 + 1000)


def test_update_dead_is_noop_when_suppress_dead_false(health):
    """Slow tier passes suppress_dead=False: a 404/410 must not suppress (recovery
    only re-probes ats connectors), so the dead path is skipped entirely."""
    for _ in range(DEAD_AFTER_CYCLES + 1):
        update_poll_health(
            health, [("slow:agg", "dead")], health.tracked_names(), suppress_dead=False)
    assert health.suppressed_names() == set()
    assert "slow:agg" not in health.tracked_names()


@pytest.fixture
def health():
    yield SqliteConnectorHealthStore(connect(":memory:"))


def test_update_suppresses_after_threshold_consecutive_dead(health):
    for _ in range(DEAD_AFTER_CYCLES):
        update_poll_health(health, [("greenhouse:dead", "dead")], health.tracked_names())
    assert health.suppressed_names() == {"greenhouse:dead"}


def test_update_does_not_suppress_below_threshold(health):
    for _ in range(DEAD_AFTER_CYCLES - 1):
        update_poll_health(health, [("greenhouse:dead", "dead")], health.tracked_names())
    assert health.suppressed_names() == set()
    assert "greenhouse:dead" in health.tracked_names()


def test_update_blocked_tracks_on_slow_tier_where_dead_would_not(health):
    """The hiring.cafe failure mode: a slow-tier connector 403s every cycle.
    'dead' is skipped entirely on slow (suppress_dead=False), so a block routed
    down that path would leave no trace. 'blocked' must still register."""
    for _ in range(DEAD_AFTER_CYCLES):
        update_poll_health(
            health, [("hiringcafe", "blocked")], health.tracked_names(), suppress_dead=False)
    assert "hiringcafe" in health.tracked_names()


def test_update_blocked_never_auto_suppresses(health):
    """A WAF rule or challenge can be lifted as easily as applied, so a block
    stays visible-but-polling rather than being retired like a dead board."""
    for _ in range(DEAD_AFTER_CYCLES * 2):
        update_poll_health(health, [("hiringcafe", "blocked")], health.tracked_names())
    assert health.suppressed_names() == set()
    assert "hiringcafe" in health.tracked_names()


def test_update_ok_clears_a_previously_blocked_connector(health):
    """Recovery path: the block lifts, the next good poll clears the row."""
    update_poll_health(health, [("hiringcafe", "blocked")], health.tracked_names())
    update_poll_health(health, [("hiringcafe", "ok")], health.tracked_names())
    assert "hiringcafe" not in health.tracked_names()


def test_update_ok_clears_tracked_connector(health):
    update_poll_health(health, [("greenhouse:x", "dead")], health.tracked_names())  # streak 1
    update_poll_health(health, [("greenhouse:x", "ok")], health.tracked_names())     # clears
    assert health.tracked_names() == set()


def test_update_ok_is_noop_when_not_tracked(health, monkeypatch):
    calls = {"clear": 0}
    real_clear = health.clear
    monkeypatch.setattr(health, "clear", lambda n: (calls.__setitem__("clear", calls["clear"] + 1), real_clear(n))[1])
    update_poll_health(health, [("greenhouse:healthy", "ok")], health.tracked_names())
    assert calls["clear"] == 0  # write-on-change: no row → no clear


def test_update_transient_is_noop(health):
    update_poll_health(health, [("greenhouse:flaky", "transient")], health.tracked_names())
    assert health.tracked_names() == set()  # no row created


def test_update_mixed_batch_is_fail_soft(health, monkeypatch):
    """A mixed batch applies each outcome independently; a store error on one
    connector is logged and swallowed, never aborting the rest of the batch."""
    health.record_dead("g:clearme")  # streak 1 → tracked, for the ok-clear path

    real_record_dead = health.record_dead

    def flaky(name):
        if name == "g:boom":
            raise RuntimeError("ddb down")
        return real_record_dead(name)

    monkeypatch.setattr(health, "record_dead", flaky)

    update_poll_health(
        health,
        [("g:boom", "dead"), ("g:newdead", "dead"), ("g:clearme", "ok"), ("g:flaky", "transient")],
        {"g:clearme"},
    )

    tracked = health.tracked_names()
    assert "g:newdead" in tracked      # processed AFTER the swallowed error → fail-soft works
    assert "g:clearme" not in tracked  # cleared (was tracked, outcome ok)
    assert "g:boom" not in tracked     # its record_dead raised → no row written


from src.poll_health import recover_suppressed


class _Conn:
    tier = "ats"

    def __init__(self, name, behavior):
        self.name = name
        self._behavior = behavior  # "ok" | "dead" | "transient"

    async def fetch(self, client, state):
        if self._behavior == "ok":
            return FetchResult(postings=[], new_state=None, not_modified=False)
        if self._behavior == "dead":
            req = httpx.Request("GET", "https://x")
            raise httpx.HTTPStatusError("gone", request=req, response=httpx.Response(410, request=req))
        raise httpx.PoolTimeout("pool")


@pytest.mark.asyncio
async def test_recover_clears_now_healthy_keeps_still_failing(health, monkeypatch):
    for name in ("g:back", "g:stilldead", "g:flaky"):
        health.record_dead(name)
        health.mark_suppressed(name)
    conns = [
        _Conn("g:back", "ok"),
        _Conn("g:stilldead", "dead"),
        _Conn("g:flaky", "transient"),
        _Conn("g:notsuppressed", "ok"),  # not in suppressed set → ignored
    ]
    monkeypatch.setattr("src.connectors.base.build_connectors", lambda *a, **k: conns)
    async with httpx.AsyncClient() as client:
        await recover_suppressed(cfg=object(), discovered=None, health=health, client=client)
    assert health.suppressed_names() == {"g:stilldead", "g:flaky"}
    assert "g:back" not in health.tracked_names()


@pytest.mark.asyncio
async def test_recover_noop_and_skips_build_when_nothing_suppressed(health, monkeypatch):
    calls = {"build": 0}

    def fake_build(*a, **k):
        calls["build"] += 1
        return []

    monkeypatch.setattr("src.connectors.base.build_connectors", fake_build)
    async with httpx.AsyncClient() as client:
        await recover_suppressed(cfg=object(), discovered=None, health=health, client=client)
    assert calls["build"] == 0  # early-returned before building any connectors


@pytest.mark.asyncio
async def test_recover_clears_orphaned_suppressed_when_connector_no_longer_built(health, monkeypatch):
    """A connector suppressed then removed from config (no longer built) is an
    orphan that can never recover — recovery clears it instead of leaving a phantom."""
    health.record_dead("g:removed")
    health.mark_suppressed("g:removed")
    monkeypatch.setattr("src.connectors.base.build_connectors", lambda *a, **k: [])  # slug gone
    async with httpx.AsyncClient() as client:
        await recover_suppressed(cfg=object(), discovered=None, health=health, client=client)
    assert health.suppressed_names() == set()  # orphan dropped


def test_recover_suppressed_accepts_boards_kwarg():
    import inspect
    from src.poll_health import recover_suppressed
    assert "boards" in inspect.signature(recover_suppressed).parameters
