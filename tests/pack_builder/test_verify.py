import asyncio

import httpx
import pytest
import respx

from scripts.pack_builder import verify as V
from scripts.pack_builder.candidates import Candidate
from src.discovery import ProbeThrottled
from src.models import FetchResult, RawPosting


def _post(loc, company=None):
    return RawPosting(source="x:y", external_id=loc or "n", title="t", description="d",
                      apply_url="https://x", location=loc, company=company)


def _status_error(code, headers=None):
    req = httpx.Request("GET", "https://x")
    return httpx.HTTPStatusError("e", request=req,
                                 response=httpx.Response(code, request=req, headers=headers or {}))


class _Stub:
    """A connector whose fetch replays a script of results/exceptions."""
    def __init__(self, name, script):
        self.name, self._script, self.calls = name, list(script), 0

    async def fetch(self, client, state):
        self.calls += 1
        step = self._script.pop(0)
        if isinstance(step, BaseException):
            raise step
        return FetchResult(postings=step)


def _patch(monkeypatch, stub):
    monkeypatch.setattr(V, "connector_from_identity", lambda family, identity, company=None: stub)


async def _no_sleep(_s):
    return None


@pytest.mark.asyncio
async def test_live_board_counts_regions_and_names_company(monkeypatch):
    stub = _Stub("ashby:acme", [[_post("San Francisco, CA", "Acme Inc"),
                                 _post("Berlin, Germany"), _post("Remote")]])
    _patch(monkeypatch, stub)
    r = await V.Verifier(None, sleep=_no_sleep).check(Candidate("ashby", {"slug": "acme"}))
    assert (r.status, r.postings, r.us_postings, r.eu_postings) == ("live", 3, 1, 1)
    assert r.company == "Acme Inc" and r.connector_name == "ashby:acme"
    assert r.identity == {"slug": "acme"}


@pytest.mark.asyncio
async def test_404_is_dead(monkeypatch):
    _patch(monkeypatch, _Stub("ashby:gone", [_status_error(404)]))
    r = await V.Verifier(None, sleep=_no_sleep).check(Candidate("ashby", {"slug": "gone"}))
    assert r.status == "dead"


@pytest.mark.asyncio
async def test_empty_board_is_dead(monkeypatch):
    _patch(monkeypatch, _Stub("ashby:empty", [[]]))
    r = await V.Verifier(None, sleep=_no_sleep).check(Candidate("ashby", {"slug": "empty"}))
    assert r.status == "dead"


@pytest.mark.asyncio
async def test_429_honours_retry_after_then_succeeds(monkeypatch):
    stub = _Stub("ashby:busy", [_status_error(429, {"Retry-After": "7"}), [_post("Austin, TX")]])
    _patch(monkeypatch, stub)
    slept = []

    async def sleep(s):
        slept.append(s)

    r = await V.Verifier(None, sleep=sleep).check(Candidate("ashby", {"slug": "busy"}))
    assert r.status == "live" and slept == [7.0]


@pytest.mark.asyncio
async def test_retry_after_is_capped(monkeypatch):
    _patch(monkeypatch, _Stub("ashby:busy", [_status_error(429, {"Retry-After": "900"}),
                                             [_post("Austin, TX")]]))
    slept = []

    async def sleep(s):
        slept.append(s)

    await V.Verifier(None, sleep=sleep).check(Candidate("ashby", {"slug": "busy"}))
    assert slept == [V.MAX_RETRY_AFTER]


@pytest.mark.asyncio
async def test_429_three_times_is_deferred(monkeypatch):
    stub = _Stub("ashby:busy", [_status_error(429)] * 3)
    _patch(monkeypatch, stub)
    r = await V.Verifier(None, sleep=_no_sleep).check(Candidate("ashby", {"slug": "busy"}))
    assert r.status == "deferred" and stub.calls == V.MAX_ATTEMPTS


@pytest.mark.asyncio
async def test_transport_error_and_5xx_are_retried(monkeypatch):
    stub = _Stub("ashby:flaky", [httpx.ConnectTimeout("t"), _status_error(503), [_post("NYC, NY")]])
    _patch(monkeypatch, stub)
    r = await V.Verifier(None, sleep=_no_sleep).check(Candidate("ashby", {"slug": "flaky"}))
    assert r.status == "live"


@pytest.mark.asyncio
async def test_unreachable_host_is_dead_without_retry(monkeypatch):
    """DNS failure / connection refused: a dead tenant host never resolves,
    so it must not count toward the partial-build guard on every run."""
    stub = _Stub("workday:gone:S", [httpx.ConnectError("[Errno 8] nodename nor servname")])
    _patch(monkeypatch, stub)
    r = await V.Verifier(None, sleep=_no_sleep).check(Candidate("ashby", {"slug": "gone"}))
    assert r.status == "dead" and stub.calls == 1


@pytest.mark.parametrize("exc", [httpx.ConnectTimeout("t"), httpx.ReadTimeout("t"),
                                 httpx.ReadError("reset"), httpx.RemoteProtocolError("eof")])
@pytest.mark.asyncio
async def test_timeouts_and_other_transport_errors_are_retried_then_deferred(monkeypatch, exc):
    stub = _Stub("ashby:slow", [exc] * V.MAX_ATTEMPTS)
    _patch(monkeypatch, stub)
    r = await V.Verifier(None, sleep=_no_sleep).check(Candidate("ashby", {"slug": "slow"}))
    assert r.status == "deferred" and stub.calls == V.MAX_ATTEMPTS


@pytest.mark.asyncio
async def test_workable_throttled_probe_is_deferred(monkeypatch):
    class _Workable:
        name = "workable:acme"
        def __init__(self, slug): pass
        async def fetch(self, client, state):
            raise ProbeThrottled("workable", "acme")
    monkeypatch.setattr(V, "WorkableConnector", _Workable)
    r = await V.Verifier(None, sleep=_no_sleep).check(Candidate("workable", {"slug": "acme"}))
    assert r.status == "deferred"


@pytest.mark.asyncio
async def test_greenhouse_company_from_board_info(monkeypatch):
    _patch(monkeypatch, _Stub("greenhouse:acme", [[_post("Seattle, WA")]]))
    with respx.mock:
        respx.get("https://boards-api.greenhouse.io/v1/boards/acme").respond(
            200, json={"name": "Acme Robotics"})
        async with httpx.AsyncClient() as client:
            r = await V.Verifier(client, sleep=_no_sleep).check(
                Candidate("greenhouse", {"slug": "acme"}))
    assert r.company == "Acme Robotics"


@pytest.mark.asyncio
async def test_company_falls_back_to_hint_then_humanized_slug(monkeypatch):
    _patch(monkeypatch, _Stub("lever:acme-corp", [[_post("Seattle, WA")], [_post("Seattle, WA")]]))
    v = V.Verifier(None, sleep=_no_sleep)
    hinted = await v.check(Candidate("lever", {"slug": "acme-corp"}, company_hint="ACME"))
    bare = await v.check(Candidate("lever", {"slug": "acme-corp"}))
    assert hinted.company == "ACME" and bare.company == "Acme Corp"


@pytest.mark.asyncio
async def test_eightfold_identity_is_resolved_first(monkeypatch):
    async def fake_verify(client, family, identity):
        identity.update(domain="acme.com", flavor="pcsx")
        return 2
    monkeypatch.setattr(V, "verify_identity", fake_verify)
    seen = {}

    def factory(family, identity, company=None):
        seen.update(identity)
        return _Stub("eightfold:acme", [[_post("Austin, TX")]])
    monkeypatch.setattr(V, "connector_from_identity", factory)
    r = await V.Verifier(None, sleep=_no_sleep).check(
        Candidate("eightfold", {"slug": "acme", "base": "https://acme.eightfold.ai"}))
    assert seen["domain"] == "acme.com" and r.identity["domain"] == "acme.com"


@pytest.mark.asyncio
async def test_per_family_concurrency_is_respected(monkeypatch):
    in_flight = peak = 0

    class _Slow:
        name = "ashby:x"
        async def fetch(self, client, state):
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.01)
            in_flight -= 1
            return FetchResult(postings=[_post("Austin, TX")])
    monkeypatch.setattr(V, "connector_from_identity", lambda f, i, company=None: _Slow())
    v = V.Verifier(None, per_family=2, sleep=_no_sleep)
    await asyncio.gather(*(v.check(Candidate("ashby", {"slug": f"s{i}"})) for i in range(8)))
    assert peak == 2


def test_humanize():
    assert V.humanize("lever", {"slug": "acme-corp"}) == "Acme Corp"
    assert V.humanize("workday", {"tenant": "big_co", "region": "wd1", "site": "X"}) == "Big Co"
