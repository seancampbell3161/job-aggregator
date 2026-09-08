import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from src.connectors.hiringcafe import HiringCafeConnector
from src.hiringcafe import HiringCafeJob
from src.models import ConnectorState

FIXTURE = json.loads(
    (Path(__file__).parent.parent / "fixtures" / "hiringcafe_example.json").read_text()
)


def _job(ats: str | None, slug: str | None, *, eid: str = "1", url: str = "https://x") -> HiringCafeJob:
    return HiringCafeJob(
        title="Software Engineer",
        company="Test Co",
        ats_family=ats,
        ats_slug=slug,
        external_id=eid,
        apply_url=url,
        posted_at=None,
        description="",
        location=None,
        remote=None,
    )


@pytest.mark.asyncio
async def test_skips_postings_already_in_active_set():
    """When (ats, slug) is in our direct-poll set, the connector skips the posting."""
    cafe = AsyncMock()
    cafe.fetch_jobs = AsyncMock(return_value=[
        _job("greenhouse", "stripe", eid="1"),  # in active set
        _job("greenhouse", "newco", eid="2"),   # NOT in active set
    ])
    active = {("greenhouse", "stripe")}
    conn = HiringCafeConnector(cafe=cafe, active_set=active, max_postings=500)

    async with httpx.AsyncClient() as client:
        result = await conn.fetch(client, ConnectorState())

    sources = [p.source for p in result.postings]
    assert sources == ["hiringcafe:greenhouse:newco"]


@pytest.mark.asyncio
async def test_emits_postings_outside_active_set_with_branded_source():
    cafe = AsyncMock()
    cafe.fetch_jobs = AsyncMock(return_value=[_job("ashby", "newco", eid="abc", url="https://apply.example.com")])
    conn = HiringCafeConnector(cafe=cafe, active_set=set(), max_postings=500)

    async with httpx.AsyncClient() as client:
        result = await conn.fetch(client, ConnectorState())

    assert len(result.postings) == 1
    p = result.postings[0]
    assert p.source == "hiringcafe:ashby:newco"
    assert p.external_id == "abc"
    assert p.apply_url == "https://apply.example.com"
    # Hiring.cafe knows the employer; passing it through keeps normalize from
    # slug-deriving "Ashby:Newco" out of the wrapped-ATS source.
    assert p.company == "Test Co"


@pytest.mark.asyncio
async def test_blank_company_falls_back_to_slug_derivation():
    """An empty company must arrive as None so normalize's fallback runs,
    rather than as "" which would surface a nameless posting."""
    cafe = AsyncMock()
    job = replace(_job("ashby", "newco", eid="abc"), company="")
    cafe.fetch_jobs = AsyncMock(return_value=[job])
    conn = HiringCafeConnector(cafe=cafe, active_set=set(), max_postings=500)

    async with httpx.AsyncClient() as client:
        result = await conn.fetch(client, ConnectorState())

    assert result.postings[0].company is None


@pytest.mark.asyncio
async def test_caps_at_max_postings():
    cafe = AsyncMock()
    cafe.fetch_jobs = AsyncMock(return_value=[
        _job("ashby", f"co{i}", eid=str(i)) for i in range(20)
    ])
    conn = HiringCafeConnector(cafe=cafe, active_set=set(), max_postings=5)

    async with httpx.AsyncClient() as client:
        result = await conn.fetch(client, ConnectorState())

    assert len(result.postings) == 5


@pytest.mark.asyncio
async def test_skips_postings_with_no_ats_family_or_slug():
    """If Hiring.cafe omits ATS info, we skip — we can't dedup safely."""
    cafe = AsyncMock()
    cafe.fetch_jobs = AsyncMock(return_value=[
        _job(None, None, eid="1"),
        _job("greenhouse", None, eid="2"),
        _job(None, "slug", eid="3"),
    ])
    conn = HiringCafeConnector(cafe=cafe, active_set=set(), max_postings=500)

    async with httpx.AsyncClient() as client:
        result = await conn.fetch(client, ConnectorState())

    assert result.postings == []


@pytest.mark.asyncio
async def test_propagates_fetcher_failure_instead_of_returning_empty():
    """A Hiring.cafe API failure must REACH the caller.

    This connector used to swallow every exception and return an empty
    FetchResult. classify_outcome() maps a FetchResult to "ok", so a hard
    failure was indistinguishable from a healthy poll that found nothing —
    which is how a 13-day Cloudflare 403 outage (from 2026-07-14) went
    unnoticed. The orchestrator's _guarded() is what keeps a raise from
    crashing the cycle; that isolation belongs there, not here."""
    cafe = AsyncMock()
    cafe.fetch_jobs = AsyncMock(side_effect=RuntimeError("api went away"))
    conn = HiringCafeConnector(cafe=cafe, active_set=set(), max_postings=500)

    async with httpx.AsyncClient() as client:
        with pytest.raises(RuntimeError, match="api went away"):
            await conn.fetch(client, ConnectorState())


@pytest.mark.asyncio
async def test_http_403_propagates_so_poll_health_can_see_it():
    """The exact hiring.cafe failure: the raised HTTPStatusError is what lets
    classify_outcome() return "blocked" rather than "ok"."""
    from src.poll_health import classify_outcome

    req = httpx.Request("GET", "https://hiringcafe.com/")
    err = httpx.HTTPStatusError(
        "403", request=req, response=httpx.Response(403, request=req))
    cafe = AsyncMock()
    cafe.fetch_jobs = AsyncMock(side_effect=err)
    conn = HiringCafeConnector(cafe=cafe, active_set=set(), max_postings=500)

    async with httpx.AsyncClient() as client:
        with pytest.raises(httpx.HTTPStatusError) as caught:
            await conn.fetch(client, ConnectorState())

    assert classify_outcome(caught.value) == "blocked"
