"""A dry run must leave NO trace. The wizard's preview depends on it: any
state it advances is state the user's first real cycle silently loses.

Fixture doubles here are modelled on tests/test_orchestrator.py's
_StubConnector / _RecordingSink (same shape: a canned fetch(), a list that
records what it was given). They're a separate, minimal set rather than an
import from that file because the store/source_state doubles need to expose
introspection (.claims, .suppressed, .already_seen, .puts) that the real
Sqlite*Store classes test_orchestrator.py uses don't have reason to offer —
run_once only needs diff_new / claim_for_notify / mark_suppressed / put /
get_many from them, so a small in-memory fake covers the same interface.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import httpx
import pytest

from src.config import AppConfig, FiltersConfig, Secrets
from src.models import ConnectorState, FetchResult, RawPosting
from src.notify.base import NotificationPayload
from src.orchestrator import run_once


def _cfg(**kw) -> AppConfig:
    # NOTE: the posting used everywhere below is titled "Staff Engineer",
    # which normalize._seniority() buckets as "staff" (see src/normalize.py's
    # _SENIORITY_PATTERNS — "staff" is checked before "senior"/"mid"). The
    # default FiltersConfig.seniority_allow is ["mid", "senior"], which does
    # NOT include "staff" — a config built with only `titles` set would
    # reject the posting via the seniority gate, not the code under test.
    # seniority_allow must explicitly include "staff".
    filters = FiltersConfig(
        titles=["engineer"],
        seniority_allow=["mid", "senior", "staff"],
        max_age_days=None,
    )
    return AppConfig(
        filters=filters,
        secrets=Secrets(ntfy_topic_url="https://ntfy.sh/t"),
        **kw,
    )


class _OneMatchConnector:
    """Yields one posting titled 'Staff Engineer' that passes the filters in
    _cfg(), and returns a non-None new_state so a real (non-dry) run has
    something for source_state.put() to persist."""

    tier = "ats"

    def __init__(self, name: str, postings: list[RawPosting], new_state: ConnectorState | None = None):
        self.name = name
        self._postings = postings
        self._new_state = new_state if new_state is not None else ConnectorState(etag='W/"v1"')

    async def fetch(self, client, state):
        return FetchResult(postings=self._postings, new_state=self._new_state, not_modified=False)


class _FakeSink:
    def __init__(self, name="fake"):
        self.name = name
        self.sent: list[NotificationPayload] = []

    async def send(self, client, payload):
        self.sent.append(payload)


class _FakeSeenStore:
    """Minimal stand-in for SqliteSeenJobsStore covering only what run_once
    calls: diff_new, claim_for_notify, release_claim, mark_suppressed."""

    def __init__(self):
        self.already_seen: set[str] = set()
        self.claims: list[str] = []
        self.suppressed: list[str] = []

    def diff_new(self, job_ids):
        candidates = list(dict.fromkeys(job_ids))
        return [j for j in candidates if j not in self.already_seen]

    def claim_for_notify(self, job_id, *, score=None, rationale=None, gaps=None, posting=None):
        self.claims.append(job_id)
        return True

    def release_claim(self, job_id):
        pass

    def mark_suppressed(self, job_id, *, score=None, rationale=None, posting=None):
        self.suppressed.append(job_id)


class _FakeSourceStateStore:
    """Minimal stand-in for SqliteSourceStateStore covering get_many/put."""

    def __init__(self):
        self.puts: list[tuple[str, ConnectorState]] = []

    def get_many(self, names):
        return {n: ConnectorState() for n in names}

    def put(self, name, state):
        self.puts.append((name, state))


@dataclass
class _Env:
    cfg: AppConfig
    connector: _OneMatchConnector
    store: _FakeSeenStore
    source_state: _FakeSourceStateStore
    sink: _FakeSink

    async def run(self, **kwargs):
        return await run_once(
            cfg=self.cfg,
            tier="ats",
            store=self.store,
            source_state=self.source_state,
            connectors=[self.connector],
            sinks=[self.sink],
            client_factory=lambda: httpx.AsyncClient(),
            **kwargs,
        )


@pytest.fixture
def dry_run_env():
    posting = RawPosting(
        source="src", external_id="1",
        title="Staff Engineer", description="Python and Go.",
        apply_url="https://x", location="Remote",
    )
    connector = _OneMatchConnector("src", [posting])
    return _Env(
        cfg=_cfg(),
        connector=connector,
        store=_FakeSeenStore(),
        source_state=_FakeSourceStateStore(),
        sink=_FakeSink(),
    )


@pytest.mark.asyncio
async def test_dry_run_does_not_advance_source_state(dry_run_env):
    """ETag/cursor hints are cache state: advancing them in a preview means
    the next REAL cycle gets a 304 and fetches nothing, while nothing was ever
    marked seen — so those postings are never alerted at all."""
    env = dry_run_env
    await env.run(dry_run=True)
    assert env.source_state.puts == []


@pytest.mark.asyncio
async def test_real_run_still_advances_source_state(dry_run_env):
    env = dry_run_env
    await env.run(dry_run=False)
    assert env.source_state.puts != []


@pytest.mark.asyncio
async def test_dry_run_marks_nothing_seen(dry_run_env):
    env = dry_run_env
    await env.run(dry_run=True)
    assert env.store.claims == []
    assert env.store.suppressed == []


@pytest.mark.asyncio
async def test_dry_run_sends_nothing(dry_run_env):
    env = dry_run_env
    await env.run(dry_run=True)
    assert env.sink.sent == []


@pytest.mark.asyncio
async def test_dry_run_returns_what_it_would_have_sent(dry_run_env):
    env = dry_run_env
    result = await env.run(dry_run=True)
    assert [p.role for p in result.would_notify] == ["Staff Engineer"]


@pytest.mark.asyncio
async def test_a_real_run_leaves_would_notify_empty(dry_run_env):
    """Only a dry run collects payloads; a real run notifies instead."""
    env = dry_run_env
    result = await env.run(dry_run=False)
    assert result.would_notify == []


@pytest.mark.asyncio
async def test_ignore_seen_shows_postings_already_seen(dry_run_env):
    """Without this, a preview run after the poller has polled shows nothing —
    exactly when the user most wants reassurance."""
    env = dry_run_env
    env.store.already_seen = {"src:1"}
    assert (await env.run(dry_run=True)).would_notify == []
    assert (await env.run(dry_run=True, ignore_seen=True)).would_notify != []


@pytest.mark.asyncio
async def test_ignore_seen_still_writes_nothing(dry_run_env):
    env = dry_run_env
    env.store.already_seen = {"src:1"}
    await env.run(dry_run=True, ignore_seen=True)
    assert env.store.claims == [] and env.source_state.puts == []
