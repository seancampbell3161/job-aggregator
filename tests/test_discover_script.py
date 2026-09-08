import asyncio

import httpx
import pytest
import respx

from scripts.discover import probe_company


@pytest.mark.asyncio
async def test_probe_company_finds_greenhouse_match():
    """If a company's slug returns postings on Greenhouse, that ATS is reported."""
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get("https://boards-api.greenhouse.io/v1/boards/foo/jobs").respond(
                200, json={"jobs": [{"id": 1}]}
            )
            respx.get("https://api.lever.co/v0/postings/foo").respond(404)
            respx.get("https://api.ashbyhq.com/posting-api/job-board/foo").respond(404)
            respx.post("https://apply.workable.com/api/v3/accounts/foo/jobs").respond(404)
            respx.get("https://api.smartrecruiters.com/v1/companies/foo/postings").respond(404)
            results = await probe_company("foo", client=client)

    by_ats = {r.ats_family: r for r in results}
    assert "greenhouse" in by_ats
    assert by_ats["greenhouse"].posting_count == 1
    assert "lever" not in by_ats


@pytest.mark.asyncio
async def test_probe_company_ranks_by_posting_count():
    """When multiple ATSs respond, results are ordered by posting count desc."""
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get("https://boards-api.greenhouse.io/v1/boards/dual/jobs").respond(
                200, json={"jobs": [{"id": 1}, {"id": 2}]}
            )
            respx.get("https://api.lever.co/v0/postings/dual").respond(
                200, json=[{"id": 1}, {"id": 2}, {"id": 3}, {"id": 4}, {"id": 5}]
            )
            respx.get("https://api.ashbyhq.com/posting-api/job-board/dual").respond(404)
            respx.post("https://apply.workable.com/api/v3/accounts/dual/jobs").respond(404)
            respx.get("https://api.smartrecruiters.com/v1/companies/dual/postings").respond(404)
            results = await probe_company("dual", client=client)

    assert [r.ats_family for r in results] == ["lever", "greenhouse"]


@pytest.mark.asyncio
async def test_probe_company_returns_empty_when_no_match():
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get("https://boards-api.greenhouse.io/v1/boards/nope/jobs").respond(404)
            respx.get("https://api.lever.co/v0/postings/nope").respond(404)
            respx.get("https://api.ashbyhq.com/posting-api/job-board/nope").respond(404)
            respx.post("https://apply.workable.com/api/v3/accounts/nope/jobs").respond(404)
            respx.get("https://api.smartrecruiters.com/v1/companies/nope/postings").respond(404)
            results = await probe_company("nope", client=client)

    assert results == []
