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


_ORC = {"tenant": "ebuu", "region": "ap1", "site": "CX"}
_ORC_PAGE = "https://ebuu.fa.ap1.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX"


def _orc_html(site_name):
    # Trimmed from a real careers page head (2026-10-06).
    return ('<html><head><meta charset="utf-8">'
            f'<meta property="og:title" content="{site_name} Careers"/>'
            f'<meta property="og:site_name" content="{site_name}"/>'
            f'<title>{site_name}</title></head><body></body></html>')


async def _check_oracle(monkeypatch, *routes):
    _patch(monkeypatch, _Stub("oraclecloud:ebuu:CX", [[_post("Sydney, Australia")]]))
    with respx.mock:
        for url, response in routes:
            if isinstance(response, httpx.Response):
                respx.get(url).mock(return_value=response)
            else:
                respx.get(url).mock(side_effect=response)
        async with httpx.AsyncClient() as client:
            return await V.Verifier(client, sleep=_no_sleep).check(Candidate("oraclecloud", _ORC))


@pytest.mark.asyncio
async def test_oracle_company_from_careers_site_name(monkeypatch):
    r = await _check_oracle(monkeypatch, (_ORC_PAGE, httpx.Response(200, text=_orc_html("AT&amp;T"))))
    assert r.company == "AT&T"


@pytest.mark.asyncio
async def test_oracle_site_name_follows_the_default_site_redirect(monkeypatch):
    r = await _check_oracle(
        monkeypatch,
        (_ORC_PAGE, httpx.Response(302, headers={"Location": _ORC_PAGE + "_1001"})),
        (_ORC_PAGE + "_1001", httpx.Response(200, text=_orc_html("Westpac Group"))),
    )
    assert r.company == "Westpac Group"


@pytest.mark.parametrize("site_name", ["All Jobs", "External Careers", "Careers 2", ""])
@pytest.mark.asyncio
async def test_oracle_generic_site_name_falls_back_to_tenant(monkeypatch, site_name):
    """Multi-brand tenants name a site for its audience, not the company."""
    r = await _check_oracle(monkeypatch, (_ORC_PAGE, httpx.Response(200, text=_orc_html(site_name))))
    assert r.company == "Ebuu"


@pytest.mark.parametrize("response", [
    httpx.Response(500, text=_orc_html("Acme")),
    httpx.Response(200, text="<html><head></head></html>"),
    httpx.ConnectError("dns"),
])
@pytest.mark.asyncio
async def test_oracle_site_name_failure_falls_back_to_tenant(monkeypatch, response):
    r = await _check_oracle(monkeypatch, (_ORC_PAGE, response))
    assert r.status == "live" and r.company == "Ebuu"


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


async def _sleeps():
    slept = []

    async def sleep(s):
        slept.append(s)
    return slept, sleep


@pytest.mark.asyncio
async def test_oracle_400_is_a_throttle_retried_with_long_backoff(monkeypatch):
    """Oracle answers HTTP 400 when throttled (127 of 300 boards in one run,
    all live minutes later), so it must not be classified dead."""
    stub = _Stub("oraclecloud:ebuu:CX", [_status_error(400), _status_error(400),
                                         [_post("Sydney, Australia")]])
    _patch(monkeypatch, stub)
    slept, sleep = await _sleeps()
    r = await V.Verifier(None, sleep=sleep, clock=lambda: 0.0, pacer_sleep=_no_sleep).check(
        Candidate("oraclecloud", _ORC))
    assert r.status == "live" and stub.calls == 3
    assert slept == [30.0, 60.0]


@pytest.mark.asyncio
async def test_workday_400_three_times_is_deferred(monkeypatch):
    stub = _Stub("workday:acme:S", [_status_error(400)] * 3)
    _patch(monkeypatch, stub)
    r = await V.Verifier(None, sleep=_no_sleep).check(
        Candidate("workday", {"tenant": "acme", "region": "wd1", "site": "S"}))
    assert r.status == "deferred" and stub.calls == V.MAX_ATTEMPTS
    assert r.reason == "HTTP 400"


@pytest.mark.asyncio
async def test_400_from_another_family_stays_dead(monkeypatch):
    stub = _Stub("greenhouse:bad", [_status_error(400)])
    _patch(monkeypatch, stub)
    r = await V.Verifier(None, sleep=_no_sleep).check(Candidate("greenhouse", {"slug": "bad"}))
    assert r.status == "dead" and stub.calls == 1 and r.reason == "HTTP 400"


@pytest.mark.asyncio
async def test_throttle_backoff_is_capped(monkeypatch):
    monkeypatch.setattr(V, "MAX_RETRY_AFTER", 40.0)
    _patch(monkeypatch, _Stub("oraclecloud:ebuu:CX", [_status_error(400), _status_error(400),
                                                      [_post("Sydney, Australia")]]))
    slept, sleep = await _sleeps()
    await V.Verifier(None, sleep=sleep, clock=lambda: 0.0, pacer_sleep=_no_sleep).check(
        Candidate("oraclecloud", _ORC))
    assert slept == [30.0, 40.0]


def test_vendor_families_are_limited_to_four_at_a_time():
    assert V.FAMILY_LIMITS["oraclecloud"] == 4 and V.FAMILY_LIMITS["workday"] == 4


@pytest.mark.parametrize("script, status, reason", [
    ([_status_error(404)], "dead", "HTTP 404"),
    ([[]], "dead", "empty"),
    ([httpx.ConnectError("dns")], "dead", "ConnectError"),
    ([ValueError("parse")], "dead", "ValueError"),
    ([_status_error(429)] * 3, "deferred", "throttled"),
    ([_status_error(503)] * 3, "deferred", "HTTP 503"),
    ([httpx.ReadTimeout("t")] * 3, "deferred", "ReadTimeout"),
])
@pytest.mark.asyncio
async def test_non_live_results_record_a_reason(monkeypatch, script, status, reason):
    _patch(monkeypatch, _Stub("ashby:x", script))
    r = await V.Verifier(None, sleep=_no_sleep).check(Candidate("ashby", {"slug": "x"}))
    assert (r.status, r.reason) == (status, reason)


@pytest.mark.asyncio
async def test_live_result_has_no_reason(monkeypatch):
    _patch(monkeypatch, _Stub("ashby:x", [[_post("Austin, TX")]]))
    r = await V.Verifier(None, sleep=_no_sleep).check(Candidate("ashby", {"slug": "x"}))
    assert r.reason is None


@pytest.mark.asyncio
async def test_throttled_probe_reason(monkeypatch):
    class _Workable:
        name = "workable:acme"
        def __init__(self, slug): pass
        async def fetch(self, client, state):
            raise ProbeThrottled("workable", "acme")
    monkeypatch.setattr(V, "WorkableConnector", _Workable)
    r = await V.Verifier(None, sleep=_no_sleep).check(Candidate("workable", {"slug": "acme"}))
    assert (r.status, r.reason) == ("deferred", "throttled")


@pytest.mark.asyncio
async def test_unbuildable_and_unresolved_reasons(monkeypatch):
    monkeypatch.setattr(V, "connector_from_identity", lambda f, i, company=None: None)
    r = await V.Verifier(None, sleep=_no_sleep).check(Candidate("ashby", {"slug": "x"}))
    assert (r.status, r.reason) == ("dead", "unbuildable")

    async def zero(client, family, identity):
        return 0
    monkeypatch.setattr(V, "verify_identity", zero)
    r = await V.Verifier(None, sleep=_no_sleep).check(
        Candidate("eightfold", {"slug": "x", "base": "https://x.eightfold.ai"}))
    assert (r.status, r.reason) == ("dead", "resolve:0")


class _Clock:
    """A fake monotonic clock whose sleep advances it: no real time passes."""
    def __init__(self):
        self.t = 0.0
        self.slept = []

    def now(self):
        return self.t

    async def sleep(self, s):
        self.slept.append(s)
        self.t += s


@pytest.mark.asyncio
async def test_pacer_gap_starts_at_zero_and_never_sleeps():
    c = _Clock()
    p = V._Pacer(clock=c.now, sleep=c.sleep)
    for _ in range(5):
        await p.wait("ashby")
    assert c.slept == []


def test_pacer_throttle_doubles_from_floor_to_cap():
    p = V._Pacer(clock=lambda: 0.0, sleep=None)
    gaps = []
    for _ in range(7):
        p.on_throttle("workable")
        gaps.append(p.gap("workable"))
    assert gaps == [0.5, 1.0, 2.0, 4.0, 8.0, 10.0, 10.0]
    assert p.gap("ashby") == 0.0


def test_pacer_success_decays_then_snaps_to_zero():
    p = V._Pacer(clock=lambda: 0.0, sleep=None)
    p.on_throttle("workable")
    p.on_success("workable")
    assert p.gap("workable") == pytest.approx(0.45)
    for _ in range(40):
        p.on_success("workable")
    assert p.gap("workable") == 0.0


@pytest.mark.asyncio
async def test_pacer_concurrent_waiters_are_spaced_by_the_gap():
    c = _Clock()
    p = V._Pacer(clock=c.now, sleep=c.sleep)
    p.on_throttle("workable")
    p.on_throttle("workable")  # gap 1.0
    starts = []

    async def go():
        await p.wait("workable")
        starts.append(c.now())

    await asyncio.gather(*(go() for _ in range(5)))
    starts.sort()
    assert all(b - a >= 1.0 - 1e-9 for a, b in zip(starts, starts[1:]))
    assert starts[-1] - starts[0] >= 4.0 - 1e-9


@pytest.mark.asyncio
async def test_429_slows_the_same_family_but_not_another(monkeypatch):
    c = _Clock()
    _patch(monkeypatch, _Stub("recruitee:a", [_status_error(429), [_post("Austin, TX")]]))
    v = V.Verifier(None, sleep=c.sleep, clock=c.now)
    await v.check(Candidate("recruitee", {"slug": "a"}))
    assert v._pacer.gap("recruitee") > 0.0
    assert v._pacer.gap("ashby") == 0.0
    c.slept.clear()
    _patch(monkeypatch, _Stub("lever:b", [[_post("Austin, TX")]]))
    await v.check(Candidate("lever", {"slug": "b"}))
    assert c.slept == []  # another family is not paced


@pytest.mark.asyncio
async def test_throttled_family_paces_its_next_check(monkeypatch):
    backoff, pace = [], []

    async def bsleep(s):
        backoff.append(s)

    async def psleep(s):
        pace.append(s)
    stub = _Stub("recruitee:a", [_status_error(429), [_post("Austin, TX")],
                                 [_post("Austin, TX")]])
    _patch(monkeypatch, stub)
    v = V.Verifier(None, sleep=bsleep, clock=lambda: 0.0, pacer_sleep=psleep)
    await v.check(Candidate("recruitee", {"slug": "a"}))
    assert backoff == [5.0] and stub.calls == 2
    # Attempt 1 reserved [0, 0] at gap 0; the 429 set gap 0.5; attempt 2 waited
    # 0.0 (next_start 0), then success decayed the gap to 0.45.
    pace.clear()
    await v.check(Candidate("recruitee", {"slug": "a"}))
    assert pace == [pytest.approx(0.5)]  # slot reserved by attempt 2 at gap 0.5
    pace.clear()
    _patch(monkeypatch, _Stub("lever:b", [[_post("Austin, TX")]]))
    await v.check(Candidate("lever", {"slug": "b"}))
    assert pace == []


@pytest.mark.asyncio
async def test_connect_error_changes_neither_throttle_nor_success(monkeypatch):
    c = _Clock()
    _patch(monkeypatch, _Stub("recruitee:a", [_status_error(429), [_post("Austin, TX")]]))
    v = V.Verifier(None, sleep=c.sleep, clock=c.now)
    await v.check(Candidate("recruitee", {"slug": "a"}))
    before = v._pacer.gap("recruitee")
    assert before > 0.0
    _patch(monkeypatch, _Stub("recruitee:b", [httpx.ConnectError("dns")]))
    r = await v.check(Candidate("recruitee", {"slug": "b"}))
    assert r.status == "dead" and r.reason == "ConnectError"
    assert v._pacer.gap("recruitee") == before


@pytest.mark.asyncio
async def test_oracle_400_and_probe_throttle_are_throttle_signals(monkeypatch):
    c = _Clock()
    _patch(monkeypatch, _Stub("oraclecloud:ebuu:CX", [_status_error(400), [_post("Sydney")]]))
    v = V.Verifier(None, sleep=c.sleep, clock=c.now)
    await v.check(Candidate("oraclecloud", _ORC))
    assert v._pacer.gap("oraclecloud") > 0.0
    script = [ProbeThrottled("workable", "a"), [_post("Austin, TX")]]

    class _Workable:
        name = "workable:a"
        def __init__(self, slug): pass
        async def fetch(self, client, state):
            step = script.pop(0)
            if isinstance(step, BaseException):
                raise step
            return FetchResult(postings=step)
    monkeypatch.setattr(V, "WorkableConnector", _Workable)
    await v.check(Candidate("workable", {"slug": "a"}))
    assert v._pacer.gap("workable") > 0.0


@pytest.mark.asyncio
async def test_5xx_and_transport_errors_do_not_change_the_gap(monkeypatch):
    c = _Clock()
    _patch(monkeypatch, _Stub("lever:a", [_status_error(503), httpx.ReadTimeout("t"),
                                          _status_error(502)]))
    v = V.Verifier(None, sleep=c.sleep, clock=c.now)
    r = await v.check(Candidate("lever", {"slug": "a"}))
    assert r.status == "deferred"
    assert v._pacer.gap("lever") == 0.0
