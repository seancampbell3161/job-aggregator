import asyncio
import json
from dataclasses import replace
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

import boto3
import httpx
import pytest
from moto import mock_aws

from src.logging_setup import configure_logging

from src.config import (
    AppConfig, FiltersConfig, HnConfig, LocationFilterConfig,
    QuietHoursConfig, SchedulesConfig, Secrets, SourcesConfig,
)
from src.models import ConnectorState, FetchResult, RawPosting
from src.notify.base import NotificationPayload
from src.orchestrator import run_once
from src.state import SeenJobsStore


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
        from src.models import FetchResult
        return FetchResult(postings=self._postings, new_state=None, not_modified=False)


class _RecordingSink:
    def __init__(self, name="rec", fail=False):
        self.name = name
        self.fail = fail
        self.received: list[NotificationPayload] = []
    async def send(self, client, payload):
        self.received.append(payload)
        if self.fail:
            raise RuntimeError("boom")


@pytest.fixture
def store():
    with mock_aws():
        ddb = boto3.client("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName="seen_jobs_test",
            AttributeDefinitions=[{"AttributeName": "job_id", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "job_id", "KeyType": "HASH"}],
            BillingMode="PAY_PER_REQUEST",
        )
        ddb.create_table(
            TableName="source_state_test",
            AttributeDefinitions=[{"AttributeName": "connector_name", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "connector_name", "KeyType": "HASH"}],
            BillingMode="PAY_PER_REQUEST",
        )
        from src.state import SourceStateStore
        yield SeenJobsStore(table_name="seen_jobs_test"), SourceStateStore(table_name="source_state_test")


@pytest.mark.asyncio
async def test_run_once_notifies_only_new_matching(store):
    seen, src_state = store
    cfg = _cfg()
    matching = RawPosting(
        source="greenhouse:stripe", external_id="1",
        title="Senior Backend Engineer", description="Python and Go.",
        apply_url="https://x", location="Remote, US",
        posted_at=datetime(2026, 4, 30, tzinfo=timezone.utc),
        comp_min=180_000, comp_max=240_000,
    )
    rejected = RawPosting(
        source="greenhouse:stripe", external_id="2",
        title="Office Manager", description="run the office",
        apply_url="https://y", location="SF",
    )
    conn = _StubConnector("greenhouse:stripe", [matching, rejected])
    sink = _RecordingSink()

    result = await run_once(
        cfg=cfg, tier="ats", store=seen, source_state=src_state,
        connectors=[conn], sinks=[sink], client_factory=lambda: httpx.AsyncClient(),
    )

    assert len(sink.received) == 1
    assert sink.received[0].company == "Stripe"
    assert result.new_count == 2  # both postings unseen by the store
    assert result.matched_count == 1  # only one passes the filter


@pytest.mark.asyncio
async def test_run_once_does_not_renotify_already_seen(store):
    seen, src_state = store
    cfg = _cfg()
    posting = RawPosting(
        source="greenhouse:stripe", external_id="1",
        title="Senior Backend Engineer", description="Python and Go.",
        apply_url="https://x", location="Remote",
        comp_min=180_000, comp_max=240_000,
    )
    conn = _StubConnector("greenhouse:stripe", [posting])
    sink = _RecordingSink()

    await run_once(
        cfg=cfg, tier="ats", store=seen, source_state=src_state, connectors=[conn], sinks=[sink],
        client_factory=lambda: httpx.AsyncClient(),
    )
    await run_once(
        cfg=cfg, tier="ats", store=seen, source_state=src_state, connectors=[conn], sinks=[sink],
        client_factory=lambda: httpx.AsyncClient(),
    )
    assert len(sink.received) == 1


@pytest.mark.asyncio
async def test_run_once_does_not_mark_seen_when_all_sinks_fail(store):
    seen, src_state = store
    cfg = _cfg()
    posting = RawPosting(
        source="greenhouse:stripe", external_id="1",
        title="Senior Backend Engineer", description="Python and Go.",
        apply_url="https://x", location="Remote",
        comp_min=180_000, comp_max=240_000,
    )
    conn = _StubConnector("greenhouse:stripe", [posting])
    bad_sink = _RecordingSink(name="bad", fail=True)

    await run_once(
        cfg=cfg, tier="ats", store=seen, source_state=src_state, connectors=[conn], sinks=[bad_sink],
        client_factory=lambda: httpx.AsyncClient(),
    )
    new_ids = seen.diff_new(["greenhouse:stripe:1"])
    assert new_ids == ["greenhouse:stripe:1"]


@pytest.mark.asyncio
async def test_run_once_isolates_connector_failures(store):
    seen, src_state = store
    cfg = _cfg()
    good_posting = RawPosting(
        source="greenhouse:stripe", external_id="1",
        title="Senior Backend Engineer", description="Python.",
        apply_url="https://x", location="Remote", comp_min=180_000, comp_max=240_000,
    )

    class _FailingConn:
        name = "lever:bad"
        tier = "ats"
        async def fetch(self, client, state):
            raise RuntimeError("source broken")

    sink = _RecordingSink()
    result = await run_once(
        cfg=cfg, tier="ats", store=seen, source_state=src_state,
        connectors=[_FailingConn(), _StubConnector("greenhouse:stripe", [good_posting])],
        sinks=[sink], client_factory=lambda: httpx.AsyncClient(),
    )
    assert len(sink.received) == 1
    assert result.failed_sources == ["lever:bad"]


@pytest.mark.asyncio
async def test_run_once_rate_limited_is_backed_off_not_a_cycle_failure(store):
    """A 429 is managed via backoff, not a hard failure: it must not land in
    failed_sources/fetch_failures (so the cycle stays ok), but the connector is
    backed off so it's skipped next cycle."""
    import time as _time

    from src.sqlite_db import connect
    from src.state_sqlite import SqliteConnectorHealthStore

    seen, src_state = store
    cfg = _cfg()

    class _RateLimitedConn:
        name = "workable:blaze"
        tier = "slow"
        async def fetch(self, client, state):
            req = httpx.Request("POST", "https://apply.workable.com/x")
            raise httpx.HTTPStatusError(
                "429", request=req,
                response=httpx.Response(429, headers={"Retry-After": "300"}, request=req))

    health = SqliteConnectorHealthStore(connect(":memory:"))
    result = await run_once(
        cfg=cfg, tier="slow", store=seen, source_state=src_state,
        connectors=[_RateLimitedConn()], sinks=[],
        client_factory=lambda: httpx.AsyncClient(), health=health,
    )
    assert result.failed_sources == []        # not counted as a hard failure
    assert result.fetch_failures == []        # → cycle stays ok (heartbeat green)
    assert "workable:blaze" in health.backoff_names(int(_time.time() * 1000))  # but backed off


@pytest.mark.asyncio
async def test_run_once_dry_run_skips_notify_and_seen(store):
    seen, src_state = store
    cfg = _cfg()
    posting = RawPosting(
        source="greenhouse:stripe", external_id="1",
        title="Senior Backend Engineer", description="Python.",
        apply_url="https://x", location="Remote", comp_min=180_000, comp_max=240_000,
    )
    conn = _StubConnector("greenhouse:stripe", [posting])
    sink = _RecordingSink()

    result = await run_once(
        cfg=cfg, tier="ats", store=seen, source_state=src_state, connectors=[conn], sinks=[sink],
        client_factory=lambda: httpx.AsyncClient(), dry_run=True,
    )
    assert sink.received == []
    assert result.matched_count == 1
    assert seen.diff_new(["greenhouse:stripe:1"]) == ["greenhouse:stripe:1"]


class _StateRecordingConnector:
    """Records the state it was given and returns whatever new_state we configure."""
    tier = "ats"

    def __init__(self, name: str, postings: list, new_state: ConnectorState | None = None):
        self.name = name
        self._postings = postings
        self._new_state = new_state
        self.received_state: ConnectorState | None = None

    async def fetch(self, client, state):
        self.received_state = state
        return FetchResult(postings=self._postings, new_state=self._new_state, not_modified=False)


@pytest.mark.asyncio
async def test_run_once_passes_persisted_state_to_connector(store):
    seen, src_state = store
    src_state.put("greenhouse:stripe", ConnectorState(etag='W/"abc"', last_modified="GMT"))
    cfg = _cfg()
    conn = _StateRecordingConnector("greenhouse:stripe", postings=[])

    await run_once(
        cfg=cfg, tier="ats", store=seen, source_state=src_state,
        connectors=[conn], sinks=[], client_factory=lambda: httpx.AsyncClient(),
    )
    assert conn.received_state == ConnectorState(etag='W/"abc"', last_modified="GMT")


@pytest.mark.asyncio
async def test_run_once_writes_new_state_after_200(store):
    seen, src_state = store
    cfg = _cfg()
    new_state = ConnectorState(etag='W/"xyz"', last_modified="Tue, 01 May 2026 00:00:00 GMT")
    conn = _StateRecordingConnector("greenhouse:stripe", postings=[], new_state=new_state)

    await run_once(
        cfg=cfg, tier="ats", store=seen, source_state=src_state,
        connectors=[conn], sinks=[], client_factory=lambda: httpx.AsyncClient(),
    )
    assert src_state.get("greenhouse:stripe") == new_state


@pytest.mark.asyncio
async def test_run_once_does_not_write_state_when_new_state_is_none(store):
    seen, src_state = store
    src_state.put("greenhouse:stripe", ConnectorState(etag='W/"keep"', last_modified=None))
    cfg = _cfg()
    conn = _StateRecordingConnector("greenhouse:stripe", postings=[], new_state=None)

    await run_once(
        cfg=cfg, tier="ats", store=seen, source_state=src_state,
        connectors=[conn], sinks=[], client_factory=lambda: httpx.AsyncClient(),
    )
    # State preserved; no overwrite
    assert src_state.get("greenhouse:stripe").etag == 'W/"keep"'


from unittest.mock import AsyncMock

from src.relevance import Score


@pytest.mark.asyncio
async def test_run_once_calls_relevance_scorer_for_matched_postings(store):
    seen, src_state = store
    cfg = _cfg()
    posting = RawPosting(
        source="greenhouse:stripe", external_id="1",
        title="Senior Backend Engineer", description="Python and Go.",
        apply_url="https://x", location="Remote",
        comp_min=180_000, comp_max=240_000,
    )
    conn = _StubConnector("greenhouse:stripe", [posting])
    sink = _RecordingSink()
    scorer = AsyncMock()
    scorer.score = AsyncMock(return_value=Score(value=8, rationale="Strong", is_fallback=False))

    await run_once(
        cfg=cfg, tier="ats", store=seen, source_state=src_state,
        connectors=[conn], sinks=[sink],
        client_factory=lambda: httpx.AsyncClient(),
        relevance_scorer=scorer,
    )

    assert scorer.score.await_count == 1
    assert len(sink.received) == 1
    payload = sink.received[0]
    assert payload.relevance_score == 8
    assert payload.relevance_priority == "max"  # 8 >= score_high=7


@pytest.mark.asyncio
async def test_run_once_persists_score_on_claimed_row(store):
    """The notified job's score/rationale must land on the DDB row so a high
    score can be explained after the fact."""
    seen, src_state = store
    cfg = _cfg()
    posting = RawPosting(
        source="greenhouse:stripe", external_id="1",
        title="Senior Backend Engineer", description="Python and Go.",
        apply_url="https://x", location="Remote",
        comp_min=180_000, comp_max=240_000,
    )
    conn = _StubConnector("greenhouse:stripe", [posting])
    sink = _RecordingSink()
    scorer = AsyncMock()
    scorer.score = AsyncMock(return_value=Score(value=8, rationale="Strong", is_fallback=False))

    await run_once(
        cfg=cfg, tier="ats", store=seen, source_state=src_state,
        connectors=[conn], sinks=[sink],
        client_factory=lambda: httpx.AsyncClient(),
        relevance_scorer=scorer,
    )

    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    items = ddb.Table("seen_jobs_test").scan()["Items"]
    assert len(items) == 1  # only the notified posting is claimed
    assert int(items[0]["score"]) == 8
    assert items[0]["rationale"] == "Strong"


@pytest.mark.asyncio
async def test_fetch_failure_logs_warning_without_traceback(store, capsys):
    """A connector fetch failure must not dump a full stack trace (each ~3 KB —
    the source of the CloudWatch bill). It should be a compact WARNING that still
    names the exception type for diagnosis."""
    seen, src_state = store
    cfg = _cfg()

    class _PoolTimeoutConn:
        name = "ashby:vercel"
        tier = "ats"

        async def fetch(self, client, state):
            raise httpx.PoolTimeout("pool exhausted")

    configure_logging()  # bind JSON handler to capsys-captured stderr
    await run_once(
        cfg=cfg, tier="ats", store=seen, source_state=src_state,
        connectors=[_PoolTimeoutConn()], sinks=[],
        client_factory=lambda: httpx.AsyncClient(),
    )

    err = capsys.readouterr().err
    assert "Traceback" not in err
    recs = []
    for line in err.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            recs.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    failed = [r for r in recs if r.get("message") == "fetch_failed"]
    assert len(failed) == 1
    rec = failed[0]
    assert rec["level"] == "WARNING"
    assert "exc" not in rec  # no traceback payload
    assert rec["error_type"] == "PoolTimeout"
    assert rec["source"] == "ashby:vercel"


@pytest.mark.asyncio
async def test_run_once_bounds_fetch_concurrency(store):
    """run_once must cap concurrent fetches so a large connector set cannot
    exhaust the httpx connection pool (the httpx.PoolTimeout failures)."""
    seen, src_state = store
    cfg = _cfg()
    gauge = {"current": 0, "max": 0}

    class _SlowConn:
        tier = "ats"

        def __init__(self, name):
            self.name = name

        async def fetch(self, client, state):
            gauge["current"] += 1
            gauge["max"] = max(gauge["max"], gauge["current"])
            await asyncio.sleep(0.02)
            gauge["current"] -= 1
            return FetchResult(postings=[], new_state=None, not_modified=False)

    connectors = [_SlowConn(f"ashby:c{i}") for i in range(50)]
    await run_once(
        cfg=cfg, tier="ats", store=seen, source_state=src_state,
        connectors=connectors, sinks=[],
        client_factory=lambda: httpx.AsyncClient(),
        max_concurrency=10,
    )
    assert gauge["max"] <= 10


@pytest.mark.asyncio
async def test_run_once_passes_through_when_scorer_is_none(store):
    """No scorer instance (relevance disabled) → score=None on payload."""
    seen, src_state = store
    cfg = _cfg()
    posting = RawPosting(
        source="greenhouse:stripe", external_id="1",
        title="Senior Backend Engineer", description="Python and Go.",
        apply_url="https://x", location="Remote",
        comp_min=180_000, comp_max=240_000,
    )
    conn = _StubConnector("greenhouse:stripe", [posting])
    sink = _RecordingSink()

    await run_once(
        cfg=cfg, tier="ats", store=seen, source_state=src_state,
        connectors=[conn], sinks=[sink],
        client_factory=lambda: httpx.AsyncClient(),
        relevance_scorer=None,
    )

    assert len(sink.received) == 1
    payload = sink.received[0]
    assert payload.relevance_score is None
    assert payload.relevance_priority == "default"


@pytest.mark.asyncio
async def test_run_once_calibrate_scores_seen_postings_without_notifying(store):
    """Calibrate mode: scores already-seen postings (bypasses the new-diff),
    applies NO suppression, and never notifies or writes (implies dry_run)."""
    seen, src_state = store
    cfg = _cfg()
    posting = RawPosting(
        source="greenhouse:stripe", external_id="1",
        title="Senior Backend Engineer", description="Python and Go.",
        apply_url="https://x", location="Remote",
        comp_min=180_000, comp_max=240_000,
    )
    conn = _StubConnector("greenhouse:stripe", [posting])

    # Warm run: a normal cycle marks the posting as seen (notifies once).
    warm_scorer = AsyncMock()
    warm_scorer.score = AsyncMock(return_value=Score(value=8, rationale="ok", is_fallback=False))
    await run_once(
        cfg=cfg, tier="ats", store=seen, source_state=src_state,
        connectors=[conn], sinks=[_RecordingSink()],
        client_factory=lambda: httpx.AsyncClient(), relevance_scorer=warm_scorer,
    )

    # Calibrate run: posting is already seen AND would score <= score_low (3).
    cal_sink = _RecordingSink()
    cal_scorer = AsyncMock()
    cal_scorer.score = AsyncMock(return_value=Score(value=2, rationale="low", is_fallback=False))
    result = await run_once(
        cfg=cfg, tier="ats", store=seen, source_state=src_state,
        connectors=[conn], sinks=[cal_sink],
        client_factory=lambda: httpx.AsyncClient(),
        relevance_scorer=cal_scorer, calibrate=True,
    )

    assert cal_scorer.score.await_count == 1   # scored despite being already-seen
    assert cal_sink.received == []             # implies dry_run: no notifications
    assert result.matched_count == 1


@pytest.mark.asyncio
async def test_run_once_calibrate_no_scorer_returns_without_error(store):
    """Calibrate with no scorer instance returns cleanly (degenerate but safe)."""
    seen, src_state = store
    cfg = _cfg()
    posting = RawPosting(
        source="greenhouse:stripe", external_id="1",
        title="Senior Backend Engineer", description="Python.",
        apply_url="https://x", location="Remote", comp_min=180_000, comp_max=240_000,
    )
    conn = _StubConnector("greenhouse:stripe", [posting])
    result = await run_once(
        cfg=cfg, tier="ats", store=seen, source_state=src_state,
        connectors=[conn], sinks=[_RecordingSink()],
        client_factory=lambda: httpx.AsyncClient(),
        relevance_scorer=None, calibrate=True,
    )
    assert result.matched_count == 1


@pytest.mark.asyncio
async def test_run_once_calibrate_caps_matched_not_raw_fetched(store):
    """The calibration cap applies AFTER the keyword filter. A matching posting
    sitting beyond the first _CALIBRATION_SAMPLE_CAP fetched postings must still
    be scored. Regression: the cap previously truncated the raw fetched set
    pre-filter, so when the first N postings were all non-matching (the common
    case — the filter passes a small fraction) calibration scored nothing."""
    from src.orchestrator import _CALIBRATION_SAMPLE_CAP

    seen, src_state = store
    cfg = _cfg()
    # (cap + 5) non-matching postings, then ONE matching posting last, so the
    # match sits beyond the cap in fetch order.
    postings = [
        RawPosting(
            source="greenhouse:stripe", external_id=f"junk{i}",
            title="Office Manager", description="run the office",
            apply_url=f"https://x/{i}", location="SF",
        )
        for i in range(_CALIBRATION_SAMPLE_CAP + 5)
    ]
    postings.append(RawPosting(
        source="greenhouse:stripe", external_id="match1",
        title="Senior Backend Engineer", description="Python and Go.",
        apply_url="https://x/match", location="Remote",
        comp_min=180_000, comp_max=240_000,
    ))
    conn = _StubConnector("greenhouse:stripe", postings)

    scorer = AsyncMock()
    scorer.score = AsyncMock(return_value=Score(value=6, rationale="ok", is_fallback=False))
    result = await run_once(
        cfg=cfg, tier="ats", store=seen, source_state=src_state,
        connectors=[conn], sinks=[_RecordingSink()],
        client_factory=lambda: httpx.AsyncClient(),
        relevance_scorer=scorer, calibrate=True,
    )

    assert result.matched_count == 1       # the lone match is found...
    assert scorer.score.await_count == 1   # ...and scored, despite being past the cap


@pytest.mark.asyncio
async def test_run_once_calibrate_caps_matched_count(store):
    """Calibrate scores at most _CALIBRATION_SAMPLE_CAP matched postings."""
    from src.orchestrator import _CALIBRATION_SAMPLE_CAP

    seen, src_state = store
    cfg = _cfg()
    postings = [
        RawPosting(
            source="greenhouse:stripe", external_id=f"m{i}",
            title="Senior Backend Engineer", description="Python and Go.",
            apply_url=f"https://x/{i}", location="Remote",
            comp_min=180_000, comp_max=240_000,
        )
        for i in range(_CALIBRATION_SAMPLE_CAP + 10)
    ]
    conn = _StubConnector("greenhouse:stripe", postings)
    scorer = AsyncMock()
    scorer.score = AsyncMock(return_value=Score(value=7, rationale="ok", is_fallback=False))
    result = await run_once(
        cfg=cfg, tier="ats", store=seen, source_state=src_state,
        connectors=[conn], sinks=[_RecordingSink()],
        client_factory=lambda: httpx.AsyncClient(),
        relevance_scorer=scorer, calibrate=True,
    )

    assert result.matched_count == _CALIBRATION_SAMPLE_CAP
    assert scorer.score.await_count == _CALIBRATION_SAMPLE_CAP


@pytest.mark.asyncio
async def test_run_once_computes_and_surfaces_gaps(store):
    from src.gaps import Gaps
    seen, src_state = store
    cfg = _cfg()
    posting = RawPosting(
        source="greenhouse:stripe", external_id="1",
        title="Senior Backend Engineer", description="Python and Go.",
        apply_url="https://x", location="Remote",
        comp_min=180_000, comp_max=240_000,
    )
    conn = _StubConnector("greenhouse:stripe", [posting])
    sink = _RecordingSink()
    scorer = AsyncMock()
    scorer.score = AsyncMock(return_value=Score(value=8, rationale="Strong", is_fallback=False))
    analyzer = AsyncMock()
    analyzer.analyze = AsyncMock(return_value=Gaps(skills=["Kubernetes"], is_fallback=False))

    await run_once(
        cfg=cfg, tier="ats", store=seen, source_state=src_state,
        connectors=[conn], sinks=[sink], client_factory=lambda: httpx.AsyncClient(),
        relevance_scorer=scorer, gap_analyzer=analyzer,
    )

    assert analyzer.analyze.await_count == 1
    assert sink.received[0].gaps == ["Kubernetes"]
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    items = ddb.Table("seen_jobs_test").scan()["Items"]
    assert items[0]["gaps"] == ["Kubernetes"]


@pytest.mark.asyncio
async def test_run_once_skips_gap_analysis_for_suppressed_postings(store):
    """A posting suppressed by score_low must never reach the gap analyzer."""
    from src.gaps import Gaps
    seen, src_state = store
    cfg = _cfg()  # score_low defaults to 3
    posting = RawPosting(
        source="greenhouse:stripe", external_id="1",
        title="Senior Backend Engineer", description="Python and Go.",
        apply_url="https://x", location="Remote",
        comp_min=180_000, comp_max=240_000,
    )
    conn = _StubConnector("greenhouse:stripe", [posting])
    sink = _RecordingSink()
    scorer = AsyncMock()
    scorer.score = AsyncMock(return_value=Score(value=2, rationale="low", is_fallback=False))  # <= score_low
    analyzer = AsyncMock()
    analyzer.analyze = AsyncMock(return_value=Gaps(skills=["x"], is_fallback=False))

    await run_once(
        cfg=cfg, tier="ats", store=seen, source_state=src_state,
        connectors=[conn], sinks=[sink], client_factory=lambda: httpx.AsyncClient(),
        relevance_scorer=scorer, gap_analyzer=analyzer,
    )

    assert sink.received == []
    assert analyzer.analyze.await_count == 0


@pytest.mark.asyncio
async def test_run_once_gap_fallback_does_not_block_notify(store):
    """A failed gap analysis (fallback) must still notify, with no gaps."""
    from src.gaps import Gaps
    seen, src_state = store
    cfg = _cfg()
    posting = RawPosting(
        source="greenhouse:stripe", external_id="1",
        title="Senior Backend Engineer", description="Python and Go.",
        apply_url="https://x", location="Remote",
        comp_min=180_000, comp_max=240_000,
    )
    conn = _StubConnector("greenhouse:stripe", [posting])
    sink = _RecordingSink()
    analyzer = AsyncMock()
    analyzer.analyze = AsyncMock(return_value=Gaps(skills=[], is_fallback=True))

    await run_once(
        cfg=cfg, tier="ats", store=seen, source_state=src_state,
        connectors=[conn], sinks=[sink], client_factory=lambda: httpx.AsyncClient(),
        gap_analyzer=analyzer,
    )

    assert len(sink.received) == 1
    assert sink.received[0].gaps == []
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    item = ddb.Table("seen_jobs_test").scan()["Items"][0]
    assert "gaps" not in item


@pytest.mark.asyncio
async def test_invocation_done_log_includes_tier(store, capsys):
    """The cycle-summary log must carry its tier so the ops dashboard can group
    per-tier cycle stats from CloudWatch."""
    seen, src_state = store
    cfg = _cfg()
    configure_logging()
    await run_once(
        cfg=cfg, tier="ats", store=seen, source_state=src_state,
        connectors=[], sinks=[], client_factory=lambda: httpx.AsyncClient(),
    )
    err = capsys.readouterr().err
    recs = []
    for line in err.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            recs.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    done = [r for r in recs if r.get("message") == "invocation_done"]
    assert len(done) == 1
    assert done[0]["tier"] == "ats"


@pytest.mark.asyncio
async def test_run_once_persists_display_fields_on_claimed_row(store):
    """The notified job's row must carry the display fields the triage UI renders."""
    seen, src_state = store
    cfg = _cfg()
    posting = RawPosting(
        source="greenhouse:stripe", external_id="1",
        title="Senior Backend Engineer", description="Python and Go.",
        apply_url="https://x", location="Remote, US",
        posted_at=datetime(2026, 4, 30, tzinfo=timezone.utc),
        comp_min=180_000, comp_max=240_000,
    )
    conn = _StubConnector("greenhouse:stripe", [posting])
    await run_once(
        cfg=cfg, tier="ats", store=seen, source_state=src_state,
        connectors=[conn], sinks=[_RecordingSink()],
        client_factory=lambda: httpx.AsyncClient(),
    )
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    item = ddb.Table("seen_jobs_test").scan()["Items"][0]
    assert item["title"] == "Senior Backend Engineer"
    assert item["company"] == "Stripe"
    assert item["location_text"]  # normalized from RawPosting.location
    assert item["apply_url"] == "https://x"
    assert item["source"] == "greenhouse:stripe"
    assert int(item["comp_min"]) == 180_000


@pytest.mark.asyncio
async def test_run_once_records_llm_failures_by_stage(store):
    """Fallback scores/gaps are collected into RunResult.llm_failures with stage+error_type."""
    from src.relevance import Score
    from src.gaps import Gaps
    seen, src_state = store
    cfg = _cfg()
    posting = RawPosting(
        source="greenhouse:stripe", external_id="1",
        title="Senior Backend Engineer", description="Python and Go.",
        apply_url="https://x", location="Remote",
        comp_min=180_000, comp_max=240_000,
    )
    conn = _StubConnector("greenhouse:stripe", [posting])
    sink = _RecordingSink()
    scorer = AsyncMock()
    scorer.score = AsyncMock(return_value=Score(value=None, rationale="(LLM unavailable)", is_fallback=True, error_type="ConnectError"))
    analyzer = AsyncMock()
    analyzer.analyze = AsyncMock(return_value=Gaps(skills=[], is_fallback=True, error_type="ConnectError"))

    result = await run_once(
        cfg=cfg, tier="ats", store=seen, source_state=src_state,
        connectors=[conn], sinks=[sink], client_factory=lambda: httpx.AsyncClient(),
        relevance_scorer=scorer, gap_analyzer=analyzer,
    )

    # A fallback score (value None) is never suppressed, so it reaches the gap stage:
    assert result.llm_failures == [
        {"stage": "relevance", "error_type": "ConnectError"},
        {"stage": "gap", "error_type": "ConnectError"},
    ]


@pytest.mark.asyncio
async def test_run_once_no_llm_failures_when_healthy(store):
    from src.relevance import Score
    from src.gaps import Gaps
    seen, src_state = store
    cfg = _cfg()
    posting = RawPosting(
        source="greenhouse:stripe", external_id="1",
        title="Senior Backend Engineer", description="Python and Go.",
        apply_url="https://x", location="Remote",
        comp_min=180_000, comp_max=240_000,
    )
    conn = _StubConnector("greenhouse:stripe", [posting])
    sink = _RecordingSink()
    scorer = AsyncMock()
    scorer.score = AsyncMock(return_value=Score(value=8, rationale="Strong", is_fallback=False))
    analyzer = AsyncMock()
    analyzer.analyze = AsyncMock(return_value=Gaps(skills=["Kubernetes"], is_fallback=False))

    result = await run_once(
        cfg=cfg, tier="ats", store=seen, source_state=src_state,
        connectors=[conn], sinks=[sink], client_factory=lambda: httpx.AsyncClient(),
        relevance_scorer=scorer, gap_analyzer=analyzer,
    )
    assert result.llm_failures == []


@pytest.mark.asyncio
async def test_run_once_marks_suppressed_posting_and_stops_rescoring(store):
    """A matched posting scored <= score_low must be recorded in seen_jobs
    (notified=False, carrying its score + display fields for the audit view)
    and must NOT be re-scored on the next cycle."""
    seen, src_state = store
    cfg = _cfg()  # score_low defaults to 3
    posting = RawPosting(
        source="greenhouse:stripe", external_id="1",
        title="Senior Backend Engineer", description="Python and Go.",
        apply_url="https://x", location="Remote",
        comp_min=180_000, comp_max=240_000,
    )
    conn = _StubConnector("greenhouse:stripe", [posting])
    sink = _RecordingSink()
    scorer = AsyncMock()
    scorer.score = AsyncMock(return_value=Score(value=2, rationale="low", is_fallback=False))

    # First cycle: posting is suppressed (score 2 <= score_low 3).
    await run_once(
        cfg=cfg, tier="ats", store=seen, source_state=src_state,
        connectors=[conn], sinks=[sink], client_factory=lambda: httpx.AsyncClient(),
        relevance_scorer=scorer,
    )
    assert sink.received == []                 # suppressed: not notified
    assert scorer.score.await_count == 1

    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    item = ddb.Table("seen_jobs_test").get_item(Key={"job_id": "greenhouse:stripe:1"})["Item"]
    assert item["notified"] is False
    assert int(item["score"]) == 2
    assert item["title"] == "Senior Backend Engineer"  # audit view, not triage

    # Second cycle: diff_new excludes it → not re-scored.
    r2 = await run_once(
        cfg=cfg, tier="ats", store=seen, source_state=src_state,
        connectors=[conn], sinks=[sink], client_factory=lambda: httpx.AsyncClient(),
        relevance_scorer=scorer,
    )
    assert scorer.score.await_count == 1       # unchanged: NOT re-scored
    assert r2.new_count == 0


@pytest.mark.asyncio
async def test_run_once_dry_run_does_not_mark_suppressed(store):
    """Dry-run must not write the suppressed marker (no DDB mutation)."""
    seen, src_state = store
    cfg = _cfg()
    posting = RawPosting(
        source="greenhouse:stripe", external_id="1",
        title="Senior Backend Engineer", description="Python and Go.",
        apply_url="https://x", location="Remote",
        comp_min=180_000, comp_max=240_000,
    )
    conn = _StubConnector("greenhouse:stripe", [posting])
    scorer = AsyncMock()
    scorer.score = AsyncMock(return_value=Score(value=2, rationale="low", is_fallback=False))

    await run_once(
        cfg=cfg, tier="ats", store=seen, source_state=src_state,
        connectors=[conn], sinks=[_RecordingSink()],
        client_factory=lambda: httpx.AsyncClient(),
        relevance_scorer=scorer, dry_run=True,
    )
    assert seen.diff_new(["greenhouse:stripe:1"]) == ["greenhouse:stripe:1"]  # nothing written


def _make_health():
    """Create a connector_health table in the ambient moto mock and return a store."""
    boto3.client("dynamodb", region_name="us-east-1").create_table(
        TableName="connector_health_test",
        AttributeDefinitions=[{"AttributeName": "connector_name", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "connector_name", "KeyType": "HASH"}],
        BillingMode="PAY_PER_REQUEST",
    )
    from src.state import ConnectorHealthStore
    return ConnectorHealthStore(table_name="connector_health_test")


class _DeadConnector:
    tier = "ats"

    def __init__(self, name, status=404):
        self.name = name
        self._status = status

    async def fetch(self, client, state):
        req = httpx.Request("GET", "https://x")
        raise httpx.HTTPStatusError("dead", request=req, response=httpx.Response(self._status, request=req))


@pytest.mark.asyncio
async def test_run_once_trips_suppression_after_three_dead_cycles(store):
    from src.poll_health import DEAD_AFTER_CYCLES
    seen, src_state = store
    health = _make_health()
    cfg = _cfg()
    conn = _DeadConnector("greenhouse:deadco", status=404)
    for _ in range(DEAD_AFTER_CYCLES):
        await run_once(
            cfg=cfg, tier="ats", store=seen, source_state=src_state,
            connectors=[conn], sinks=[], client_factory=lambda: httpx.AsyncClient(),
            health=health,
        )
    assert health.suppressed_names() == {"greenhouse:deadco"}


@pytest.mark.asyncio
async def test_run_once_transient_failure_never_trips(store):
    from src.poll_health import DEAD_AFTER_CYCLES
    seen, src_state = store
    health = _make_health()
    cfg = _cfg()

    class _TimeoutConn:
        name = "greenhouse:flaky"
        tier = "ats"
        async def fetch(self, client, state):
            raise httpx.PoolTimeout("pool")

    for _ in range(DEAD_AFTER_CYCLES + 2):
        await run_once(
            cfg=cfg, tier="ats", store=seen, source_state=src_state,
            connectors=[_TimeoutConn()], sinks=[], client_factory=lambda: httpx.AsyncClient(),
            health=health,
        )
    assert health.suppressed_names() == set()


@pytest.mark.asyncio
async def test_run_once_success_clears_dead_streak(store):
    seen, src_state = store
    health = _make_health()
    cfg = _cfg()
    await run_once(
        cfg=cfg, tier="ats", store=seen, source_state=src_state,
        connectors=[_DeadConnector("greenhouse:x", status=404)], sinks=[],
        client_factory=lambda: httpx.AsyncClient(), health=health,
    )
    assert "greenhouse:x" in health.tracked_names()
    await run_once(
        cfg=cfg, tier="ats", store=seen, source_state=src_state,
        connectors=[_StubConnector("greenhouse:x", [])], sinks=[],
        client_factory=lambda: httpx.AsyncClient(), health=health,
    )
    assert health.tracked_names() == set()


@pytest.mark.asyncio
async def test_run_once_dry_run_does_not_update_health(store):
    from src.poll_health import DEAD_AFTER_CYCLES
    seen, src_state = store
    health = _make_health()
    cfg = _cfg()
    for _ in range(DEAD_AFTER_CYCLES):
        await run_once(
            cfg=cfg, tier="ats", store=seen, source_state=src_state,
            connectors=[_DeadConnector("greenhouse:x")], sinks=[],
            client_factory=lambda: httpx.AsyncClient(), health=health, dry_run=True,
        )
    assert health.tracked_names() == set()


@pytest.mark.asyncio
async def test_run_once_without_health_does_not_raise(store):
    seen, src_state = store
    cfg = _cfg()
    await run_once(
        cfg=cfg, tier="ats", store=seen, source_state=src_state,
        connectors=[_DeadConnector("greenhouse:x")], sinks=[],
        client_factory=lambda: httpx.AsyncClient(),
    )  # health=None → no tracking, no error


@pytest.mark.asyncio
async def test_run_once_sets_tailor_url_when_endpoint_configured(store):
    from src.config import Secrets
    seen, src_state = store
    base = _cfg()
    cfg = base.model_copy(update={"secrets": Secrets(
        ntfy_topic_url="x", discord_webhook_url="x",
        tailor_endpoint_url="https://ep.example/tailor", tailor_signing_secret="sek")})
    matching = RawPosting(
        source="greenhouse:stripe", external_id="1",
        title="Senior Backend Engineer", description="Python and Go.",
        apply_url="https://x", location="Remote, US",
        posted_at=datetime(2026, 4, 30, tzinfo=timezone.utc),
        comp_min=180_000, comp_max=240_000,
    )
    rejected = RawPosting(
        source="greenhouse:stripe", external_id="2",
        title="Office Manager", description="run the office",
        apply_url="https://y", location="SF",
    )
    conn = _StubConnector("greenhouse:stripe", [matching, rejected])
    sink = _RecordingSink()
    await run_once(cfg=cfg, tier="ats", store=seen, source_state=src_state,
                   connectors=[conn], sinks=[sink],
                   client_factory=lambda: httpx.AsyncClient())
    assert sink.received, "expected one notification"
    assert sink.received[0].tailor_url and "https://ep.example/tailor?job_id=" in sink.received[0].tailor_url


@pytest.mark.asyncio
async def test_run_once_no_tailor_url_when_endpoint_unconfigured(store):
    seen, src_state = store
    matching = RawPosting(
        source="greenhouse:stripe", external_id="1",
        title="Senior Backend Engineer", description="Python and Go.",
        apply_url="https://x", location="Remote, US",
        posted_at=datetime(2026, 4, 30, tzinfo=timezone.utc),
        comp_min=180_000, comp_max=240_000,
    )
    rejected = RawPosting(
        source="greenhouse:stripe", external_id="2",
        title="Office Manager", description="run the office",
        apply_url="https://y", location="SF",
    )
    conn = _StubConnector("greenhouse:stripe", [matching, rejected])
    sink = _RecordingSink()
    await run_once(cfg=_cfg(), tier="ats", store=seen, source_state=src_state,
                   connectors=[conn], sinks=[sink],
                   client_factory=lambda: httpx.AsyncClient())
    assert sink.received and sink.received[0].tailor_url is None


class _EnrichConnector(_StubConnector):
    supports_enrich = True
    def __init__(self, name, postings, *, fail=False):
        super().__init__(name, postings)
        self._fail = fail
    async def enrich(self, client, posting):
        if self._fail:
            raise RuntimeError("detail boom")
        return replace(posting, description="ENRICHED JD")


def _matching_raw(source="workday:acme:External", ext="1"):
    return RawPosting(
        source=source, external_id=ext,
        title="Senior Backend Engineer", description="Python and Go.",
        apply_url="https://x", location="Remote, US",
        posted_at=datetime(2026, 4, 30, tzinfo=timezone.utc),
        comp_min=180_000, comp_max=240_000,
    )


@pytest.mark.asyncio
async def test_run_once_enriches_survivors_before_storing(store):
    seen, src_state = store
    conn = _EnrichConnector("workday:acme:External", [_matching_raw()])
    await run_once(
        cfg=_cfg(), tier="ats", store=seen, source_state=src_state,
        connectors=[conn], sinks=[_RecordingSink()],
        client_factory=lambda: httpx.AsyncClient(),
    )
    jd = seen.get_jd("workday:acme:External:1")
    assert jd is not None and jd.description == "ENRICHED JD"


@pytest.mark.asyncio
async def test_run_once_does_not_enrich_plain_connectors(store):
    seen, src_state = store
    conn = _StubConnector("greenhouse:stripe", [_matching_raw(source="greenhouse:stripe")])
    await run_once(
        cfg=_cfg(), tier="ats", store=seen, source_state=src_state,
        connectors=[conn], sinks=[_RecordingSink()],
        client_factory=lambda: httpx.AsyncClient(),
    )
    jd = seen.get_jd("greenhouse:stripe:1")
    assert jd is not None and jd.description == "Python and Go."  # original, not enriched


@pytest.mark.asyncio
async def test_run_once_enrich_failure_keeps_coarse(store):
    seen, src_state = store
    conn = _EnrichConnector("workday:acme:External", [_matching_raw()], fail=True)
    await run_once(
        cfg=_cfg(), tier="ats", store=seen, source_state=src_state,
        connectors=[conn], sinks=[_RecordingSink()],
        client_factory=lambda: httpx.AsyncClient(),
    )
    jd = seen.get_jd("workday:acme:External:1")
    assert jd is not None and jd.description == "Python and Go."  # fail-soft → coarse kept


class _RecordingRejected:
    def __init__(self, fail=False):
        self.records: list[tuple[str, str]] = []
        self.fail = fail
    def record(self, posting, *, rejected_by):
        if self.fail:
            raise RuntimeError("audit db down")
        self.records.append((posting.job_id, rejected_by))
        return True


class _FakeScore:
    def __init__(self, value, rationale="Weak fit"):
        self.value = value
        self.rationale = rationale
        self.is_fallback = False
        self.error_type = None


class _StubScorer:
    def __init__(self, value):
        self._value = value
    async def score(self, posting):
        return _FakeScore(self._value)


@pytest.mark.asyncio
async def test_filter_rejections_recorded_with_gate(store):
    seen, src_state = store
    cfg = _cfg()
    matching = RawPosting(
        source="greenhouse:stripe", external_id="1",
        title="Senior Backend Engineer", description="Python and Go.",
        apply_url="https://x", location="Remote, US",
        comp_min=180_000, comp_max=240_000,
    )
    wrong_title = RawPosting(
        source="greenhouse:stripe", external_id="2",
        title="Office Manager", description="run the office",
        apply_url="https://y", location="Remote, US",
    )
    rejected = _RecordingRejected()
    await run_once(
        cfg=cfg, tier="ats", store=seen, source_state=src_state,
        connectors=[_StubConnector("greenhouse:stripe", [matching, wrong_title])],
        sinks=[_RecordingSink()], client_factory=lambda: httpx.AsyncClient(),
        rejected_store=rejected,
    )
    assert rejected.records == [("greenhouse:stripe:2", "role")]


@pytest.mark.asyncio
async def test_rejection_capture_skipped_on_dry_run_and_none_store(store):
    seen, src_state = store
    cfg = _cfg()
    wrong_title = RawPosting(
        source="greenhouse:stripe", external_id="2",
        title="Office Manager", description="run the office",
        apply_url="https://y", location="Remote, US",
    )
    rejected = _RecordingRejected()
    await run_once(
        cfg=cfg, tier="ats", store=seen, source_state=src_state,
        connectors=[_StubConnector("greenhouse:stripe", [wrong_title])],
        sinks=[_RecordingSink()], client_factory=lambda: httpx.AsyncClient(),
        rejected_store=rejected, dry_run=True,
    )
    assert rejected.records == []
    # rejected_store=None must not raise
    await run_once(
        cfg=cfg, tier="ats", store=seen, source_state=src_state,
        connectors=[_StubConnector("greenhouse:stripe", [wrong_title])],
        sinks=[_RecordingSink()], client_factory=lambda: httpx.AsyncClient(),
    )


@pytest.mark.asyncio
async def test_rejection_capture_failure_does_not_break_cycle(store):
    seen, src_state = store
    cfg = _cfg()
    matching = RawPosting(
        source="greenhouse:stripe", external_id="1",
        title="Senior Backend Engineer", description="Python and Go.",
        apply_url="https://x", location="Remote, US",
        comp_min=180_000, comp_max=240_000,
    )
    wrong_title = RawPosting(
        source="greenhouse:stripe", external_id="2",
        title="Office Manager", description="run the office",
        apply_url="https://y", location="Remote, US",
    )
    sink = _RecordingSink()
    result = await run_once(
        cfg=cfg, tier="ats", store=seen, source_state=src_state,
        connectors=[_StubConnector("greenhouse:stripe", [matching, wrong_title])],
        sinks=[sink], client_factory=lambda: httpx.AsyncClient(),
        rejected_store=_RecordingRejected(fail=True),
    )
    assert result.notified_count == 1  # cycle survived the audit failure
    assert len(sink.received) == 1


@pytest.mark.asyncio
async def test_suppressed_row_carries_rationale_and_title(store):
    seen, src_state = store
    cfg = _cfg()
    matching = RawPosting(
        source="greenhouse:stripe", external_id="1",
        title="Senior Backend Engineer", description="Python and Go.",
        apply_url="https://x", location="Remote, US",
        comp_min=180_000, comp_max=240_000,
    )
    await run_once(
        cfg=cfg, tier="ats", store=seen, source_state=src_state,
        connectors=[_StubConnector("greenhouse:stripe", [matching])],
        sinks=[_RecordingSink()], client_factory=lambda: httpx.AsyncClient(),
        relevance_scorer=_StubScorer(cfg.relevance.score_low),  # at threshold -> suppressed
    )
    item = seen._table.get_item(Key={"job_id": "greenhouse:stripe:1"})["Item"]
    assert item["notified"] is False
    assert item["rationale"] == "Weak fit"
    assert item["title"] == "Senior Backend Engineer"


@pytest.mark.asyncio
async def test_run_once_isolates_malformed_posting(store):
    """A posting that blows up in normalize() must cost only that posting.

    Regression for the 2026-08-02 outage: an Oracle requisition with a null
    Title raised out of run_once, so every ats cycle died for 12h — before
    record_cycle(), which is why /pipeline and the zero-yield alerts never
    saw it. The good postings in the same batch must still flow."""
    seen, src_state = store
    cfg = _cfg()
    good = RawPosting(
        source="greenhouse:stripe", external_id="1",
        title="Senior Backend Engineer", description="Python and Go.",
        apply_url="https://x", location="Remote, US",
        comp_min=180_000, comp_max=240_000,
    )
    # title=None is what the connector bug produced; object() is a stand-in for
    # any future payload shape normalize() can't handle at all.
    malformed = RawPosting(
        source="oraclecloud:estm:CX_1", external_id="2",
        title=object(), description="", apply_url="https://y", location="SF",
    )
    conn = _StubConnector("greenhouse:stripe", [malformed, good])
    sink = _RecordingSink()

    result = await run_once(
        cfg=cfg, tier="ats", store=seen, source_state=src_state,
        connectors=[conn], sinks=[sink], client_factory=lambda: httpx.AsyncClient(),
    )

    # the cycle survived and the good posting still notified
    assert len(sink.received) == 1
    assert sink.received[0].role == "Senior Backend Engineer"
    assert result.matched_count == 1
    # the bad one was skipped and recorded, without flipping source poll-health
    assert len(result.normalize_failures) == 1
    assert result.normalize_failures[0]["source"] == "oraclecloud:estm:CX_1"
    assert result.failed_sources == []
    assert result.fetch_failures == []


@pytest.mark.asyncio
async def test_run_once_null_title_posting_flows_through(store):
    """The specific Oracle payload: a null title normalizes to "" and is simply
    filtered out on the merits, not skipped as an error."""
    seen, src_state = store
    cfg = _cfg()
    null_title = RawPosting(
        source="oraclecloud:estm:CX_1", external_id="2",
        title=None, description="", apply_url="https://y", location="SF",
    )
    conn = _StubConnector("oraclecloud:estm:CX_1", [null_title])

    result = await run_once(
        cfg=cfg, tier="ats", store=seen, source_state=src_state,
        connectors=[conn], sinks=[_RecordingSink()], client_factory=lambda: httpx.AsyncClient(),
    )

    assert result.normalize_failures == []   # normalize handled it
    assert result.new_count == 1
    assert result.matched_count == 0         # empty title fails the title filter
