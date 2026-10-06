"""run_once processes each board as its fetch completes. These tests pin the
cycle's outputs (so streaming changes cost, not results) and bound peak
memory by board count."""
from datetime import time
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
