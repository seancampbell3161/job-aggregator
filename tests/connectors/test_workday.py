import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
import respx

from src.connectors.workday import WorkdayConnector
from src.models import ConnectorState, NormalizedPosting

FIXTURE = json.loads(
    (Path(__file__).parent.parent / "fixtures" / "workday_jobs_example.json").read_text()
)


@pytest.mark.asyncio
async def test_workday_fetch_returns_postings():
    conn = WorkdayConnector(tenant="salesforce", region="wd12", site="External_Career_Site")
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.post(
                "https://salesforce.wd12.myworkdayjobs.com/wday/cxs/salesforce/External_Career_Site/jobs"
            ).respond(200, json=FIXTURE)
            result = await conn.fetch(client, ConnectorState())

    assert len(result.postings) == len(FIXTURE["jobPostings"])
    p0 = result.postings[0]
    assert p0.source == "workday:salesforce:External_Career_Site"
    assert p0.title == FIXTURE["jobPostings"][0]["title"]
    # The public URL must include the career-site segment; the site-less form
    # ("{base}{externalPath}") 404s with a generic Workday "page not found".
    assert p0.apply_url == (
        "https://salesforce.wd12.myworkdayjobs.com/External_Career_Site"
        + FIXTURE["jobPostings"][0]["externalPath"]
    )
    assert p0.location == FIXTURE["jobPostings"][0]["locationsText"]
    # bulletFields[0] is the JR-prefixed job id, preferred over externalPath suffix.
    assert p0.external_id == FIXTURE["jobPostings"][0]["bulletFields"][0]


@pytest.mark.asyncio
async def test_workday_parses_posted_today_as_now():
    """`postedOn` is human text like 'Posted Today' or 'Posted 5 Days Ago'.
    Today/yesterday should resolve to a recent timestamp so the age filter
    treats them as fresh."""
    fixture = {
        "total": 1,
        "jobPostings": [{
            "title": "Senior Software Engineer",
            "externalPath": "/job/x/Senior-Software-Engineer_JR0001",
            "locationsText": "Remote - US",
            "postedOn": "Posted Today",
            "bulletFields": ["JR0001"],
        }],
    }
    conn = WorkdayConnector(tenant="acme", region="wd1", site="External")
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.post(
                "https://acme.wd1.myworkdayjobs.com/wday/cxs/acme/External/jobs"
            ).respond(200, json=fixture)
            result = await conn.fetch(client, ConnectorState())

    p = result.postings[0]
    assert p.posted_at is not None
    age_seconds = (datetime.now(timezone.utc) - p.posted_at).total_seconds()
    assert 0 <= age_seconds < 60, "Posted Today should resolve to ~now"


@pytest.mark.asyncio
async def test_workday_parses_n_days_ago():
    fixture = {
        "total": 1,
        "jobPostings": [{
            "title": "SWE",
            "externalPath": "/job/x/SWE_JR0002",
            "locationsText": "USA",
            "postedOn": "Posted 5 Days Ago",
            "bulletFields": ["JR0002"],
        }],
    }
    conn = WorkdayConnector(tenant="acme", region="wd1", site="External")
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.post(
                "https://acme.wd1.myworkdayjobs.com/wday/cxs/acme/External/jobs"
            ).respond(200, json=fixture)
            result = await conn.fetch(client, ConnectorState())

    p = result.postings[0]
    assert p.posted_at is not None
    age_days = (datetime.now(timezone.utc) - p.posted_at).days
    assert age_days == 5


@pytest.mark.asyncio
async def test_workday_parses_30_plus_days_ago():
    fixture = {
        "total": 1,
        "jobPostings": [{
            "title": "SWE",
            "externalPath": "/job/x/SWE_JR0003",
            "locationsText": "USA",
            "postedOn": "Posted 30+ Days Ago",
            "bulletFields": ["JR0003"],
        }],
    }
    conn = WorkdayConnector(tenant="acme", region="wd1", site="External")
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.post(
                "https://acme.wd1.myworkdayjobs.com/wday/cxs/acme/External/jobs"
            ).respond(200, json=fixture)
            result = await conn.fetch(client, ConnectorState())

    p = result.postings[0]
    assert p.posted_at is not None
    assert (datetime.now(timezone.utc) - p.posted_at).days == 30


@pytest.mark.asyncio
async def test_workday_external_id_falls_back_to_path_suffix():
    """If bulletFields is empty, the connector must still produce a stable
    external_id derived from the externalPath."""
    fixture = {
        "total": 1,
        "jobPostings": [{
            "title": "SWE",
            "externalPath": "/job/x/Some-Role_JR9999",
            "locationsText": "USA",
            "postedOn": "Posted Today",
            "bulletFields": [],
        }],
    }
    conn = WorkdayConnector(tenant="acme", region="wd1", site="External")
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.post(
                "https://acme.wd1.myworkdayjobs.com/wday/cxs/acme/External/jobs"
            ).respond(200, json=fixture)
            result = await conn.fetch(client, ConnectorState())

    p = result.postings[0]
    assert p.external_id == "Some-Role_JR9999"


def _make_postings(n: int, start: int) -> list[dict]:
    return [
        {
            "title": "Senior Software Engineer",
            "externalPath": f"/job/x/Role_JR{start + i}",
            "locationsText": "Remote - US",
            "postedOn": "Posted Today",
            "bulletFields": [f"JR{start + i}"],
        }
        for i in range(n)
    ]


@pytest.mark.asyncio
async def test_workday_paginates_until_partial_page():
    # total=45 → pages of 20, 20, 5 (the 5-item page is partial → stop).
    pages = {
        0: {"total": 45, "jobPostings": _make_postings(20, 0)},
        20: {"total": 45, "jobPostings": _make_postings(20, 20)},
        40: {"total": 45, "jobPostings": _make_postings(5, 40)},
    }

    def _by_offset(request: httpx.Request) -> httpx.Response:
        offset = json.loads(request.content)["offset"]
        return httpx.Response(200, json=pages[offset])

    conn = WorkdayConnector(tenant="acme", region="wd1", site="External")
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.post(
                "https://acme.wd1.myworkdayjobs.com/wday/cxs/acme/External/jobs"
            ).mock(side_effect=_by_offset)
            result = await conn.fetch(client, ConnectorState())

    assert len(result.postings) == 45
    assert result.postings[0].external_id == "JR0"
    assert result.postings[-1].external_id == "JR44"


@pytest.mark.asyncio
async def test_workday_later_page_error_returns_partial():
    # Page 0 is a full 20 (so the loop advances); page 1 errors → return page 0.
    def _err_on_second(request: httpx.Request) -> httpx.Response:
        offset = json.loads(request.content)["offset"]
        if offset == 0:
            return httpx.Response(200, json={"total": 45, "jobPostings": _make_postings(20, 0)})
        return httpx.Response(500)

    conn = WorkdayConnector(tenant="acme", region="wd1", site="External")
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.post(
                "https://acme.wd1.myworkdayjobs.com/wday/cxs/acme/External/jobs"
            ).mock(side_effect=_err_on_second)
            result = await conn.fetch(client, ConnectorState())

    assert len(result.postings) == 20  # partial result, no crash


@pytest.mark.asyncio
async def test_workday_later_page_bad_json_returns_partial():
    # Page 0: full 20-item JSON response; page 1: HTTP 200 but non-JSON body.
    # The connector should return the 20 postings already collected and not raise.
    def _bad_json_on_second(request: httpx.Request) -> httpx.Response:
        offset = json.loads(request.content)["offset"]
        if offset == 0:
            return httpx.Response(200, json={"total": 40, "jobPostings": _make_postings(20, 0)})
        return httpx.Response(200, text="not json")

    conn = WorkdayConnector(tenant="acme", region="wd1", site="External")
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.post(
                "https://acme.wd1.myworkdayjobs.com/wday/cxs/acme/External/jobs"
            ).mock(side_effect=_bad_json_on_second)
            result = await conn.fetch(client, ConnectorState())

    assert len(result.postings) == 20  # partial result, no raise


@pytest.mark.asyncio
async def test_workday_first_page_error_raises():
    conn = WorkdayConnector(tenant="acme", region="wd1", site="External")
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.post(
                "https://acme.wd1.myworkdayjobs.com/wday/cxs/acme/External/jobs"
            ).respond(500)
            with pytest.raises(httpx.HTTPStatusError):
                await conn.fetch(client, ConnectorState())


def _posting(apply_url: str, description: str = "JR thin") -> NormalizedPosting:
    return NormalizedPosting(
        job_id="workday:acme:External:JR1", title="Senior Software Engineer",
        company="Acme", location_text="2 Locations", location_tags=frozenset(),
        seniority="senior", stack=frozenset(), comp_min=None, comp_max=None,
        apply_url=apply_url, description=description, posted_at=None,
        source="workday:acme:External",
    )


@pytest.mark.asyncio
async def test_workday_enrich_fills_description_and_location():
    conn = WorkdayConnector(tenant="acme", region="wd1", site="External")
    # apply_url now carries the "/External" site segment; enrich must strip it
    # back off so the CXS detail path isn't ".../External/External/...".
    posting = _posting("https://acme.wd1.myworkdayjobs.com/External/job/x/Role_JR1")
    detail = {
        "jobPostingInfo": {
            "jobDescription": "<p>Build <b>Python</b> services.</p>",
            "location": "San Francisco, CA",
            "additionalLocations": ["New York, NY"],
        }
    }
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get(
                "https://acme.wd1.myworkdayjobs.com/wday/cxs/acme/External/job/x/Role_JR1"
            ).respond(200, json=detail)
            out = await conn.enrich(client, posting)

    assert "Build Python services." in out.description
    assert "San Francisco, CA" in out.location_text
    assert "New York, NY" in out.location_text
    assert out.job_id == posting.job_id          # other fields preserved
    assert out.title == posting.title


@pytest.mark.asyncio
async def test_workday_enrich_tolerates_legacy_siteless_apply_url():
    # Rows stored before the site prefix was added lack "/External"; enrich must
    # still resolve them to the same CXS detail URL.
    conn = WorkdayConnector(tenant="acme", region="wd1", site="External")
    posting = _posting("https://acme.wd1.myworkdayjobs.com/job/x/Role_JR1")
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get(
                "https://acme.wd1.myworkdayjobs.com/wday/cxs/acme/External/job/x/Role_JR1"
            ).respond(200, json={"jobPostingInfo": {"jobDescription": "<p>Python</p>"}})
            out = await conn.enrich(client, posting)
    assert "Python" in out.description


@pytest.mark.asyncio
async def test_workday_enrich_failsoft_returns_original_on_error():
    conn = WorkdayConnector(tenant="acme", region="wd1", site="External")
    posting = _posting("https://acme.wd1.myworkdayjobs.com/External/job/x/Role_JR1")
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get(
                "https://acme.wd1.myworkdayjobs.com/wday/cxs/acme/External/job/x/Role_JR1"
            ).respond(500)
            out = await conn.enrich(client, posting)

    assert out is posting  # unchanged, no crash


def test_workday_supports_enrich_flag():
    assert WorkdayConnector(tenant="a", region="wd1", site="E").supports_enrich is True
