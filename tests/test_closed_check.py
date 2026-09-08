# tests/test_closed_check.py
from datetime import datetime, timezone

import httpx
import pytest
import respx

from src.closed_check import check_posting, run_closed_sweep
from src.models import NormalizedPosting
from src.sqlite_db import connect
from src.state_sqlite import SqliteSeenJobsStore


def _card(source, job_id, apply_url):
    return {"job_id": job_id, "source": source, "apply_url": apply_url}


# apply_url now carries the "/External" career-site segment; the older siteless
# form (WD_LEGACY) must still resolve to the same CXS detail URL.
WD = _card(
    "workday:acme:External", "workday:acme:External:JR1",
    "https://acme.wd1.myworkdayjobs.com/External/job/NYC/Eng_JR1",
)
WD_LEGACY = _card(
    "workday:acme:External", "workday:acme:External:JR1",
    "https://acme.wd1.myworkdayjobs.com/job/NYC/Eng_JR1",
)
_WD_DETAIL = "https://acme.wd1.myworkdayjobs.com/wday/cxs/acme/External/job/NYC/Eng_JR1"


@pytest.mark.asyncio
async def test_workday_alive_and_miss_and_empty():
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get(_WD_DETAIL).respond(200, json={"jobPostingInfo": {"id": "JR1"}})
            assert await check_posting(client, WD) == "alive"
        with respx.mock:
            respx.get(_WD_DETAIL).respond(404)
            assert await check_posting(client, WD) == "miss"
        with respx.mock:
            respx.get(_WD_DETAIL).respond(200, json={})
            assert await check_posting(client, WD) == "miss"


@pytest.mark.asyncio
async def test_workday_legacy_siteless_apply_url_resolves():
    # The "/External" site prefix is stripped only when present; a siteless
    # legacy URL must hit the same detail endpoint (no doubled site segment).
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get(_WD_DETAIL).respond(200, json={"jobPostingInfo": {"id": "JR1"}})
            assert await check_posting(client, WD_LEGACY) == "alive"


ORC = _card(
    "oraclecloud:egug:CX_1", "oraclecloud:egug:CX_1:12345",
    "https://egug.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/12345",
)
_ORC_DETAIL = "https://egug.fa.us2.oraclecloud.com/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"


@pytest.mark.asyncio
async def test_oraclecloud_alive_and_miss():
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get(url__startswith=_ORC_DETAIL).respond(
                200, json={"items": [{"ExternalDescriptionStr": "<p>JD</p>"}]})
            assert await check_posting(client, ORC) == "alive"
        with respx.mock:
            respx.get(url__startswith=_ORC_DETAIL).respond(200, json={"items": []})
            assert await check_posting(client, ORC) == "miss"


GH = _card("greenhouse:stripe", "greenhouse:stripe:123", "https://boards.greenhouse.io/stripe/jobs/123")


@pytest.mark.asyncio
async def test_greenhouse_alive_and_miss():
    url = "https://boards-api.greenhouse.io/v1/boards/stripe/jobs/123"
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get(url).respond(200, json={"id": 123})
            assert await check_posting(client, GH) == "alive"
        with respx.mock:
            respx.get(url).respond(404)
            assert await check_posting(client, GH) == "miss"


LV = _card("lever:netflix", "lever:netflix:abc-def", "https://jobs.lever.co/netflix/abc-def")


@pytest.mark.asyncio
async def test_lever_alive_and_miss():
    url = "https://api.lever.co/v0/postings/netflix/abc-def"
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get(url).respond(200, json={"id": "abc-def"})
            assert await check_posting(client, LV) == "alive"
        with respx.mock:
            respx.get(url).respond(404)
            assert await check_posting(client, LV) == "miss"


FALLBACK = _card("ashby:linear", "ashby:linear:xyz", "https://jobs.ashbyhq.com/linear/xyz")


@pytest.mark.asyncio
async def test_fallback_hard_404_is_miss_soft_200_is_alive():
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get("https://jobs.ashbyhq.com/linear/xyz").respond(200, text="Job no longer available")
            assert await check_posting(client, FALLBACK) == "alive"  # soft-404: precision over recall
        with respx.mock:
            respx.get("https://jobs.ashbyhq.com/linear/xyz").respond(404)
            assert await check_posting(client, FALLBACK) == "miss"
        with respx.mock:
            respx.get("https://jobs.ashbyhq.com/linear/xyz").respond(410)
            assert await check_posting(client, FALLBACK) == "miss"


@pytest.mark.asyncio
async def test_network_error_is_unknown_and_5xx_is_unknown():
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get("https://jobs.ashbyhq.com/linear/xyz").mock(side_effect=httpx.ConnectError("down"))
            assert await check_posting(client, FALLBACK) == "unknown"
        with respx.mock:
            respx.get(_WD_DETAIL).respond(503)
            assert await check_posting(client, WD) == "unknown"


@pytest.mark.asyncio
async def test_card_without_apply_url_is_unknown():
    async with httpx.AsyncClient() as client:
        assert await check_posting(client, _card("ashby:linear", "ashby:linear:x", "")) == "unknown"


def _store_with_card(status="applied", job_id="ashby:linear:xyz",
                     apply_url="https://jobs.ashbyhq.com/linear/xyz"):
    s = SqliteSeenJobsStore(connect(":memory:"))
    posting = NormalizedPosting(
        job_id=job_id, title="Eng", company="Linear", location_text="Remote",
        location_tags=frozenset(), seniority="mid", stack=frozenset(),
        comp_min=None, comp_max=None, apply_url=apply_url, description="d",
        posted_at=datetime(2026, 7, 1, tzinfo=timezone.utc), source=job_id.rsplit(":", 1)[0],
    )
    s.claim_for_notify(job_id, score=7, posting=posting)
    s.set_status(job_id, status)
    return s


def _factory_responding(status_code):
    def _make():
        def h(request):
            return httpx.Response(status_code)
        return httpx.AsyncClient(transport=httpx.MockTransport(h))
    return _make


@pytest.mark.asyncio
async def test_sweep_two_strikes_then_flag():
    s = _store_with_card()
    r1 = await run_closed_sweep(s, client_factory=_factory_responding(404))
    assert r1 == {"checked": 1, "misses": 1, "flagged": 0, "reset": 0, "unknown": 0}
    assert s.get_match("ashby:linear:xyz")["posting_closed_at"] is None

    r2 = await run_closed_sweep(s, client_factory=_factory_responding(404))
    assert r2["flagged"] == 1
    assert s.get_match("ashby:linear:xyz")["posting_closed_at"] is not None


@pytest.mark.asyncio
async def test_sweep_alive_resets_and_unflags():
    s = _store_with_card()
    await run_closed_sweep(s, client_factory=_factory_responding(404))
    await run_closed_sweep(s, client_factory=_factory_responding(404))
    assert s.get_match("ashby:linear:xyz")["posting_closed_at"] is not None

    r = await run_closed_sweep(s, client_factory=_factory_responding(200))
    assert r["reset"] == 1
    m = s.get_match("ashby:linear:xyz")
    assert m["posting_closed_at"] is None and m["closed_misses"] == 0


@pytest.mark.asyncio
async def test_sweep_unknown_is_noop_and_archive_never_checked():
    s = _store_with_card()

    def _boom():
        def h(request):
            raise httpx.ConnectError("down")
        return httpx.AsyncClient(transport=httpx.MockTransport(h))

    r = await run_closed_sweep(s, client_factory=_boom)
    assert r["unknown"] == 1 and r["misses"] == 0
    assert s.get_match("ashby:linear:xyz")["closed_misses"] == 0

    s.set_status("ashby:linear:xyz", "rejected")  # archived
    r2 = await run_closed_sweep(s, client_factory=_factory_responding(404))
    assert r2["checked"] == 0


@pytest.mark.asyncio
async def test_sweep_skips_store_without_write_method():
    class _ReadOnly:
        def list_matches(self):
            return [{"job_id": "x", "status": "applied", "source": "a:b", "apply_url": "https://x"}]

    r = await run_closed_sweep(_ReadOnly(), client_factory=_factory_responding(404))
    assert r == {"checked": 0, "misses": 0, "flagged": 0, "reset": 0, "unknown": 0}
