import json
from pathlib import Path

import httpx
import pytest
import respx

from datetime import datetime, timedelta, timezone

from src.connectors.oraclecloud import OracleCloudConnector, _parse_posted_date
from src.models import ConnectorState, NormalizedPosting
from src.user_agent import user_agent

FIXTURE = json.loads(
    (Path(__file__).parent.parent / "fixtures" / "oraclecloud_jobs_example.json").read_text()
)
_REQS = FIXTURE["items"][0]["requisitionList"]
_API = "https://egug.fa.us2.oraclecloud.com/hcmRestApi/resources/latest/recruitingCEJobRequisitions"


def _conn(company=None):
    return OracleCloudConnector(tenant="egug", region="us2", site="CX_1", company=company)


def test_parse_posted_date_stamps_utc_on_naive_date():
    assert _parse_posted_date("2026-06-30") == datetime(2026, 6, 30, tzinfo=timezone.utc)


def test_parse_posted_date_preserves_existing_offset():
    """A future full timestamp with its own offset must not be shifted to UTC
    by blindly overwriting tzinfo."""
    minus5 = timezone(timedelta(hours=-5))
    dt = _parse_posted_date("2026-06-30T10:00:00-05:00")
    assert dt == datetime(2026, 6, 30, 10, 0, 0, tzinfo=minus5)
    assert dt.utcoffset() == timedelta(hours=-5)


@pytest.mark.asyncio
async def test_fetch_maps_fields():
    conn = _conn()
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get(url__startswith=_API).respond(200, json=FIXTURE)
            result = await conn.fetch(client, ConnectorState())

    assert len(result.postings) == len(_REQS)
    p0, r0 = result.postings[0], _REQS[0]
    assert p0.source == "oraclecloud:egug:CX_1"
    assert p0.external_id == str(r0["Id"])
    assert p0.title == r0["Title"]
    assert p0.apply_url == (
        f"https://egug.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/{r0['Id']}"
    )
    assert r0["PrimaryLocation"] in (p0.location or "")
    assert p0.posted_at is not None
    assert p0.company is None  # no display name configured for this tenant


@pytest.mark.asyncio
async def test_fetch_sets_company_from_tenant_config():
    """The optional `company` display name (config.yaml oraclecloud.company)
    flows through to every RawPosting so ORC tenants surface as e.g. 'American
    Express' instead of the opaque tenant slug 'Egug:Cx 1'."""
    conn = _conn(company="American Express")
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get(url__startswith=_API).respond(200, json=FIXTURE)
            result = await conn.fetch(client, ConnectorState())
    assert result.postings
    assert all(p.company == "American Express" for p in result.postings)


@pytest.mark.asyncio
async def test_fetch_paginates_with_cap():
    """Full pages advance the offset; the page cap bounds total requests."""
    conn = _conn()
    full_page = {
        "items": [{
            "TotalJobsCount": 1000,
            "requisitionList": [
                {"Id": str(1000 + i), "Title": f"Engineer {i}", "PostedDate": "2026-07-01",
                 "PrimaryLocation": "New York, NY, United States", "secondaryLocations": []}
                for i in range(25)
            ],
        }]
    }
    async with httpx.AsyncClient() as client:
        with respx.mock:
            route = respx.get(url__startswith=_API).respond(200, json=full_page)
            result = await conn.fetch(client, ConnectorState())

    assert route.call_count == 5          # _MAX_PAGES cap against a lying total
    assert len(result.postings) == 125    # 5 pages x 25


@pytest.mark.asyncio
async def test_fetch_stops_on_partial_page():
    conn = _conn()
    async with httpx.AsyncClient() as client:
        with respx.mock:
            route = respx.get(url__startswith=_API).respond(200, json=FIXTURE)  # 2 reqs < 25
            result = await conn.fetch(client, ConnectorState())
    assert route.call_count == 1
    assert len(result.postings) == len(_REQS)


@pytest.mark.asyncio
async def test_fetch_first_page_failure_raises():
    conn = _conn()
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get(url__startswith=_API).respond(500)
            with pytest.raises(Exception):
                await conn.fetch(client, ConnectorState())


@pytest.mark.asyncio
async def test_fetch_sends_project_user_agent_and_finder():
    conn = _conn()
    async with httpx.AsyncClient() as client:
        with respx.mock:
            route = respx.get(url__startswith=_API).respond(200, json=FIXTURE)
            await conn.fetch(client, ConnectorState())
    req = route.calls[0].request
    assert req.headers["User-Agent"] == user_agent()
    assert "findReqs;siteNumber=CX_1,sortBy=POSTING_DATES_DESC,limit=25,offset=0" in str(req.url)


_DETAIL_API = (
    "https://egug.fa.us2.oraclecloud.com/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
)


def _posting(job_id="oraclecloud:egug:CX_1:12345", description="Engineer | NYC"):
    return NormalizedPosting(
        job_id=job_id, title="Software Engineer", company="Egug",
        location_text="New York, NY", location_tags=frozenset({"new york"}),
        seniority="mid", stack=frozenset(), comp_min=None, comp_max=None,
        apply_url="https://egug.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/12345",
        description=description, posted_at=None, source="oraclecloud:egug:CX_1",
    )


@pytest.mark.asyncio
async def test_enrich_replaces_description_with_detail_jd():
    conn = _conn()
    detail = {"items": [{"ExternalDescriptionStr": "<p>Build <b>backend</b> services in Python.</p>"}]}
    async with httpx.AsyncClient() as client:
        with respx.mock:
            route = respx.get(url__startswith=_DETAIL_API).respond(200, json=detail)
            enriched = await conn.enrich(client, _posting())
    assert route.call_count == 1
    assert 'Id=%2212345%22' in str(route.calls[0].request.url) or 'Id="12345"' in str(route.calls[0].request.url)
    assert enriched.description == "Build backend services in Python."
    assert enriched.title == "Software Engineer"  # untouched fields preserved


@pytest.mark.asyncio
async def test_enrich_failure_returns_posting_unchanged():
    conn = _conn()
    p = _posting()
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get(url__startswith=_DETAIL_API).respond(500)
            enriched = await conn.enrich(client, p)
    assert enriched is p


@pytest.mark.asyncio
async def test_enrich_empty_description_returns_posting_unchanged():
    conn = _conn()
    p = _posting()
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get(url__startswith=_DETAIL_API).respond(200, json={"items": [{}]})
            enriched = await conn.enrich(client, p)
    assert enriched is p
