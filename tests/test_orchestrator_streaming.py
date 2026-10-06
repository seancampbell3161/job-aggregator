"""run_once processes each board as its fetch completes. These tests pin the
cycle's outputs (so streaming changes cost, not results) and bound peak
memory by board count."""
import asyncio
import tracemalloc
from dataclasses import replace
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

import httpx
import pytest

from src.config import (
    AppConfig, FiltersConfig, HnConfig, LocationFilterConfig,
    QuietHoursConfig, SchedulesConfig, Secrets, SourcesConfig,
)
from src.models import FetchResult, RawPosting
from src.notify.base import NotificationPayload
from src.orchestrator import run_once
from src.sqlite_db import connect
from src.state_sqlite import (
    SqliteRejectedPostingsStore, SqliteSeenJobsStore, SqliteSourceStateStore,
)


def _cfg() -> AppConfig:
    return AppConfig(
        filters=FiltersConfig(
            titles=["software engineer", "backend engineer"],
            seniority_allow=["mid", "senior"],
            location=LocationFilterConfig(
                remote_must_be_us=True, allowed_cities=["dallas", "seattle", "denver"], allow_unknown=True
            ),
            comp_floor_usd=120_000,
            stack_any_of=["python", "go"],
        ),
        quiet_hours=QuietHoursConfig(
            timezone=ZoneInfo("UTC"), start=time(23, 0), end=time(7, 0)
        ),
        sources=SourcesConfig(
            greenhouse=[], lever=[], ashby=[], workable=[],
            hn_who_is_hiring=HnConfig(enabled=False),
        ),
        schedules=SchedulesConfig(ats_minutes=2, slow_minutes=15),
        secrets=Secrets(ntfy_topic_url="x", discord_webhook_url="x"),
    )


class _StubConnector:
    tier = "ats"
    def __init__(self, name, postings):
        self.name = name
        self._postings = postings
    async def fetch(self, client, state):
        return FetchResult(postings=self._postings, new_state=None, not_modified=False)


class _FailingConnector:
    tier = "ats"
    def __init__(self, name):
        self.name = name
    async def fetch(self, client, state):
        req = httpx.Request("GET", "https://boards.example/x")
        raise httpx.HTTPStatusError(
            "503", request=req, response=httpx.Response(503, request=req))


class _RecordingSink:
    name = "rec"
    def __init__(self):
        self.received: list[NotificationPayload] = []
    async def send(self, client, payload):
        self.received.append(payload)


class _FakeScore:
    def __init__(self, value):
        self.value = value
        self.rationale = "Strong fit"
        self.is_fallback = False
        self.error_type = None


class _StubScorer:
    async def score(self, posting):
        return _FakeScore(8)


@pytest.fixture
def store():
    conn = connect(":memory:")
    yield SqliteSeenJobsStore(conn), SqliteSourceStateStore(conn)


def _match(source, ext, title="Senior Backend Engineer"):
    return RawPosting(
        source=source, external_id=ext, title=title,
        description="Python and Go.", apply_url=f"https://x/{source}/{ext}",
        location="Remote", comp_min=180_000, comp_max=240_000,
    )


def _reject(source, ext):
    return RawPosting(
        source=source, external_id=ext, title="Office Manager",
        description="run the office", apply_url=f"https://y/{source}/{ext}",
        location="SF",
    )


@pytest.mark.asyncio
async def test_cycle_outputs_are_unchanged():
    conn = connect(":memory:")
    seen, src_state = SqliteSeenJobsStore(conn), SqliteSourceStateStore(conn)
    seen.mark_seen("ashby:seenco:1", notified=True)

    connectors = [
        _StubConnector("greenhouse:alpha", [
            _match("greenhouse:alpha", "1"),
            _reject("greenhouse:alpha", "2"),
        ]),
        _FailingConnector("lever:broken"),
        _StubConnector("ashby:seenco", [
            _match("ashby:seenco", "1"),          # already in seen_jobs
            _reject("ashby:seenco", "2"),
        ]),
        # The same job_id from two connectors: notified once, counted new once.
        _StubConnector("greenhouse:gamma", [
            _match("greenhouse:gamma", "7", title="Senior Software Engineer"),
        ]),
        _StubConnector("greenhouse:gamma-mirror", [
            _match("greenhouse:gamma", "7", title="Senior Software Engineer"),
            _reject("greenhouse:gamma", "8"),
        ]),
        # hiringcafe emits sources that differ from its connector name.
        _StubConnector("hiringcafe", [
            _match("hiringcafe:greenhouse:epsilon", "3", title="Backend Engineer"),
            _reject("hiringcafe:lever:zeta", "4"),
        ]),
    ]
    sink = _RecordingSink()

    result = await run_once(
        cfg=_cfg(), tier="ats", store=seen, source_state=src_state,
        connectors=connectors, sinks=[sink],
        client_factory=lambda: httpx.AsyncClient(),
        relevance_scorer=_StubScorer(),
        rejected_store=SqliteRejectedPostingsStore(conn),
    )

    assert result.fetched_count == 9
    assert result.new_count == 7
    assert result.matched_count == 3
    assert result.notified_count == 3
    assert sorted(result.failed_sources) == ["lever:broken"]
    assert sorted(p.title for p in sink.received) == [
        "Backend Engineer @ Epsilon",
        "Senior Backend Engineer @ Alpha",
        "Senior Software Engineer @ Gamma",
    ]
    assert sorted(p.apply_url for p in sink.received) == [
        "https://x/greenhouse:alpha/1",
        "https://x/greenhouse:gamma/7",
        "https://x/hiringcafe:greenhouse:epsilon/3",
    ]
    notified_ids = sorted(
        r["job_id"] for r in conn.execute(
            "SELECT job_id FROM seen_jobs WHERE notified = 1 AND job_id != 'ashby:seenco:1'"))
    assert notified_ids == [
        "greenhouse:alpha:1",
        "greenhouse:gamma:7",
        "hiringcafe:greenhouse:epsilon:3",
    ]
    rejected_ids = sorted(
        r["job_id"] for r in conn.execute("SELECT job_id FROM rejected_postings"))
    assert rejected_ids == [
        "ashby:seenco:2",
        "greenhouse:alpha:2",
        "greenhouse:gamma:8",
        "hiringcafe:lever:zeta:4",
    ]


class _BigBoard:
    """Builds its postings at fetch time, so the fixture holds nothing."""
    tier = "ats"
    def __init__(self, name, n=40, size=20_000):
        self.name, self._n, self._size = name, n, size
    async def fetch(self, client, state):
        return FetchResult(postings=[
            RawPosting(source=self.name, external_id=str(i), title="Office Manager",
                       description="x" * self._size, apply_url=f"https://x/{i}", location="SF")
            for i in range(self._n)])


async def _peak(store, boards):
    seen, src_state = store
    tracemalloc.start()
    await run_once(cfg=_cfg(), tier="ats", store=seen, source_state=src_state,
                   connectors=[_BigBoard(f"greenhouse:b{i}") for i in range(boards)],
                   sinks=[], client_factory=lambda: httpx.AsyncClient(), max_concurrency=4)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return peak


@pytest.mark.asyncio
async def test_peak_memory_does_not_grow_with_board_count(store):
    small = await _peak(store, 10)
    large = await _peak(store, 100)
    assert large < small * 2.5     # gather-then-process would be ~10x


class _SlowConnector(_StubConnector):
    def __init__(self, name, postings, delay):
        super().__init__(name, postings)
        self._delay = delay
    async def fetch(self, client, state):
        await asyncio.sleep(self._delay)
        return await super().fetch(client, state)


@pytest.mark.asyncio
async def test_matches_keep_connector_order_not_completion_order(store):
    """Boards finish in reverse list order; alerts still go out in list order,
    so scoring-cap ties, calibrate's sample and notification order don't
    depend on which fetch happened to return first."""
    seen, src_state = store
    names = ["greenhouse:a", "greenhouse:b", "greenhouse:c"]
    connectors = [
        _SlowConnector(n, [_match(n, "1"), _match(n, "2")], delay=0.03 * (len(names) - i))
        for i, n in enumerate(names)
    ]
    sink = _RecordingSink()
    await run_once(
        cfg=_cfg(), tier="ats", store=seen, source_state=src_state,
        connectors=connectors, sinks=[sink],
        client_factory=lambda: httpx.AsyncClient(),
    )
    assert [p.apply_url for p in sink.received] == [
        f"https://x/{n}/{e}" for n in names for e in ("1", "2")
    ]


@pytest.mark.asyncio
async def test_polled_counts_postings_new_to_the_install_per_fetched_board(store):
    seen, src_state = store
    seen.mark_seen("ashby:seenco:1", notified=True)
    result = await run_once(
        cfg=_cfg(), tier="ats", store=seen, source_state=src_state,
        connectors=[
            _StubConnector("greenhouse:alpha", [
                _match("greenhouse:alpha", "1"), _reject("greenhouse:alpha", "2")]),
            _StubConnector("ashby:seenco", [
                _match("ashby:seenco", "1"), _reject("ashby:seenco", "2")]),
            _StubConnector("hiringcafe", [_reject("hiringcafe:lever:zeta", "4")]),
            _StubConnector("lever:empty", []),
            _FailingConnector("lever:broken"),
        ],
        sinks=[], client_factory=lambda: httpx.AsyncClient(),
    )
    assert result.polled == {
        "greenhouse:alpha": 2, "ashby:seenco": 1, "hiringcafe": 1, "lever:empty": 0,
    }


@pytest.mark.asyncio
async def test_deferred_sources_names_the_boards_the_scoring_cap_deferred(store):
    seen, src_state = store
    cfg = _cfg()
    cfg = cfg.model_copy(update={
        "relevance": cfg.relevance.model_copy(update={"max_scored_per_cycle": 1})})
    newest = replace(_match("greenhouse:new", "1"),
                     posted_at=datetime(2026, 9, 20, tzinfo=timezone.utc))
    older = replace(_match("hiringcafe:lever:old", "1"),
                    posted_at=datetime(2026, 9, 1, tzinfo=timezone.utc))
    result = await run_once(
        cfg=cfg, tier="ats", store=seen, source_state=src_state,
        connectors=[_StubConnector("hiringcafe", [older]),
                    _StubConnector("greenhouse:new", [newest])],
        sinks=[_RecordingSink()], client_factory=lambda: httpx.AsyncClient(),
        relevance_scorer=_StubScorer(),
    )
    assert result.deferred_sources == ["hiringcafe"]   # the connector, not the posting source


class _ExplodingSeenStore:
    def __init__(self, inner):
        self._inner = inner
    def diff_new(self, ids):
        raise RuntimeError("seen store down")
    def __getattr__(self, name):
        return getattr(self._inner, name)


@pytest.mark.asyncio
async def test_a_screening_failure_raises_and_cancels_the_fetches_still_running(store):
    seen, src_state = store
    cancelled = asyncio.Event()

    class _Hanging:
        tier = "ats"
        name = "greenhouse:slow"
        async def fetch(self, client, state):
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled.set()
                raise

    with pytest.raises(RuntimeError, match="seen store down"):
        await asyncio.wait_for(run_once(
            cfg=_cfg(), tier="ats", store=_ExplodingSeenStore(seen), source_state=src_state,
            connectors=[_Hanging(), _StubConnector("greenhouse:fast", [_match("greenhouse:fast", "1")])],
            sinks=[], client_factory=lambda: httpx.AsyncClient(),
        ), timeout=5)
    assert cancelled.is_set()


# --- evaluation memory -------------------------------------------------------

def _mem_cycle_kwargs(conn):
    return dict(
        cfg=_cfg(), tier="ats", store=SqliteSeenJobsStore(conn),
        source_state=SqliteSourceStateStore(conn),
        client_factory=lambda: httpx.AsyncClient(),
        rejected_store=SqliteRejectedPostingsStore(conn),
    )


def _spy_normalize(monkeypatch):
    import src.orchestrator as orch
    calls = []
    real = orch.normalize

    def spy(raw, **kw):
        calls.append(f"{raw.source}:{raw.external_id}")
        return real(raw, **kw)

    monkeypatch.setattr(orch, "normalize", spy)
    return calls


def _evaluated_ids(conn):
    return sorted(r["job_id"] for r in conn.execute("SELECT job_id FROM evaluated_postings"))


@pytest.mark.asyncio
async def test_rejected_posting_is_not_renormalized_next_cycle(monkeypatch):
    from src.state_sqlite import SqliteEvaluatedPostingsStore
    conn = connect(":memory:")
    ev = SqliteEvaluatedPostingsStore(conn)
    calls = _spy_normalize(monkeypatch)
    conns = [_StubConnector("greenhouse:a", [_reject("greenhouse:a", "1")])]
    kw = _mem_cycle_kwargs(conn)
    r1 = await run_once(connectors=conns, sinks=[], evaluated_store=ev, generation=1, **kw)
    assert calls == ["greenhouse:a:1"] and r1.polled["greenhouse:a"] == 1 and r1.new_count == 1
    calls.clear()
    r2 = await run_once(connectors=conns, sinks=[], evaluated_store=ev, generation=1, **kw)
    assert calls == []
    assert r2.polled["greenhouse:a"] == 0 and r2.new_count == 0


@pytest.mark.asyncio
async def test_generation_change_reevaluates(monkeypatch):
    from src.state_sqlite import SqliteEvaluatedPostingsStore
    conn = connect(":memory:")
    ev = SqliteEvaluatedPostingsStore(conn)
    calls = _spy_normalize(monkeypatch)
    conns = [_StubConnector("greenhouse:a", [_reject("greenhouse:a", "1")])]
    kw = _mem_cycle_kwargs(conn)
    await run_once(connectors=conns, sinks=[], evaluated_store=ev, generation=1, **kw)
    calls.clear()
    r2 = await run_once(connectors=conns, sinks=[], evaluated_store=ev, generation=2, **kw)
    assert calls == ["greenhouse:a:1"]
    assert r2.polled["greenhouse:a"] == 0


@pytest.mark.asyncio
async def test_matches_are_not_recorded():
    from src.state_sqlite import SqliteEvaluatedPostingsStore
    conn = connect(":memory:")
    ev = SqliteEvaluatedPostingsStore(conn)
    sink = _RecordingSink()
    conns = [_StubConnector("greenhouse:a", [_match("greenhouse:a", "1"), _reject("greenhouse:a", "2")])]
    r = await run_once(connectors=conns, sinks=[sink], evaluated_store=ev, generation=1,
                       **_mem_cycle_kwargs(conn))
    assert r.notified_count == 1
    assert conn.execute("SELECT COUNT(*) FROM seen_jobs WHERE job_id='greenhouse:a:1'").fetchone()[0] == 1
    assert _evaluated_ids(conn) == ["greenhouse:a:2"]


@pytest.mark.asyncio
async def test_ignore_seen_bypasses_memory_and_writes_nothing(monkeypatch):
    from src.state_sqlite import SqliteEvaluatedPostingsStore
    conn = connect(":memory:")
    ev = SqliteEvaluatedPostingsStore(conn)
    conns = [_StubConnector("greenhouse:a", [_match("greenhouse:a", "1"), _reject("greenhouse:a", "2")])]
    kw = _mem_cycle_kwargs(conn)
    await run_once(connectors=conns, sinks=[_RecordingSink()], evaluated_store=ev, generation=1, **kw)
    before = _evaluated_ids(conn)
    assert before == ["greenhouse:a:2"]
    calls = _spy_normalize(monkeypatch)
    r = await run_once(connectors=conns, sinks=[], evaluated_store=ev, generation=1,
                       dry_run=True, ignore_seen=True, **kw)
    assert sorted(calls) == ["greenhouse:a:1", "greenhouse:a:2"]
    assert [p.apply_url for p in r.would_notify] == ["https://x/greenhouse:a/1"]
    assert _evaluated_ids(conn) == before


@pytest.mark.asyncio
async def test_dry_run_writes_no_memory():
    from src.state_sqlite import SqliteEvaluatedPostingsStore
    conn = connect(":memory:")
    ev = SqliteEvaluatedPostingsStore(conn)
    conns = [_StubConnector("greenhouse:a", [_reject("greenhouse:a", "1")])]
    await run_once(connectors=conns, sinks=[], evaluated_store=ev, generation=1,
                   dry_run=True, **_mem_cycle_kwargs(conn))
    assert _evaluated_ids(conn) == []


# --- per-vendor pacing, concurrency cap and fetch deadline ---

def _status_error(code):
    req = httpx.Request("GET", "https://boards.example/x")
    return httpx.HTTPStatusError(str(code), request=req, response=httpx.Response(code, request=req))


class _ErrConnector:
    tier = "ats"
    def __init__(self, name, code):
        self.name, self._code = name, code
    async def fetch(self, client, state):
        raise _status_error(self._code)


class _FakeTime:
    def __init__(self):
        self.t = 0.0
        self.slept: list[float] = []
    def now(self):
        return self.t
    async def sleep(self, s):
        self.slept.append(s)
        self.t += s


def _pace_cycle(connectors, **kw):
    conn = connect(":memory:")
    return run_once(
        cfg=_cfg(), tier="ats", store=SqliteSeenJobsStore(conn),
        source_state=SqliteSourceStateStore(conn), connectors=connectors,
        sinks=[], client_factory=lambda: httpx.AsyncClient(), **kw)


@pytest.mark.asyncio
async def test_throttle_slows_vendor_and_success_recovers():
    from src.pacing import VendorPacer
    ft = _FakeTime()
    pacer = VendorPacer(clock=ft.now, sleep=ft.sleep)
    conns = [_ErrConnector("greenhouse:a", 429), _StubConnector("ashby:b", [])]
    await _pace_cycle(conns, pacer=pacer)
    assert pacer.gap("greenhouse") > 0
    assert pacer.gap("ashby") == 0
    # successes decay the gap back down
    before = pacer.gap("greenhouse")
    await _pace_cycle([_StubConnector("greenhouse:a", [])], pacer=pacer)
    assert pacer.gap("greenhouse") < before


@pytest.mark.asyncio
async def test_oracle_400_counts_as_throttle():
    from src.pacing import VendorPacer
    ft = _FakeTime()
    pacer = VendorPacer(clock=ft.now, sleep=ft.sleep)
    await _pace_cycle([_ErrConnector("oraclecloud:x", 400), _ErrConnector("greenhouse:y", 400)],
                      pacer=pacer)
    assert pacer.gap("oraclecloud") > 0
    assert pacer.gap("greenhouse") == 0


@pytest.mark.asyncio
async def test_throttle_feedback_applies_within_the_cycle():
    from src.pacing import VendorPacer
    ft = _FakeTime()
    pacer = VendorPacer(clock=ft.now, sleep=ft.sleep)
    # More boards than the vendor cap, all 429: the first wave reserves slots
    # at gap 0, later boards are queued behind the vendor semaphore and so
    # see the throttled gap and wait.
    conns = [_ErrConnector(f"greenhouse:b{i}", 429) for i in range(20)]
    await _pace_cycle(conns, pacer=pacer)
    assert any(s > 0 for s in ft.slept)


@pytest.mark.asyncio
async def test_per_vendor_concurrency_is_capped():
    from src.pacing import PER_VENDOR_CONCURRENCY
    state = {"cur": 0, "peak": 0}

    class _Slow:
        tier = "ats"
        def __init__(self, name):
            self.name = name
        async def fetch(self, client, st):
            state["cur"] += 1
            state["peak"] = max(state["peak"], state["cur"])
            await asyncio.sleep(0.01)
            state["cur"] -= 1
            return FetchResult(postings=[], new_state=None, not_modified=False)

    await _pace_cycle([_Slow(f"greenhouse:s{i}") for i in range(20)], max_concurrency=40)
    assert state["peak"] == PER_VENDOR_CONCURRENCY


@pytest.mark.asyncio
async def test_deadline_paces_out_boards_without_failure():
    import time as _time
    from src.pacing import MAX_GAP, VendorPacer
    from src.state_sqlite import SqliteConnectorHealthStore
    conn = connect(":memory:")
    health = SqliteConnectorHealthStore(conn)
    slept: list[float] = []

    async def fake_sleep(s):
        slept.append(s)  # does not advance time

    pacer = VendorPacer(clock=_time.monotonic, sleep=fake_sleep)
    for _ in range(10):
        pacer.on_throttle("greenhouse")
    assert pacer.gap("greenhouse") == MAX_GAP
    conns = [_StubConnector(f"greenhouse:p{i}", []) for i in range(4)]
    result = await run_once(
        cfg=_cfg(), tier="ats", store=SqliteSeenJobsStore(conn),
        source_state=SqliteSourceStateStore(conn), connectors=conns, sinks=[],
        client_factory=lambda: httpx.AsyncClient(), health=health,
        pacer=pacer, fetch_deadline_s=1)
    assert len(result.paced_out) == 3
    assert result.failed_sources == []
    assert result.fetch_failures == []
    assert health.tracked_names() == set()
    assert not any(n in result.polled for n in result.paced_out)
