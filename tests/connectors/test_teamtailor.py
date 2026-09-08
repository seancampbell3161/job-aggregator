from pathlib import Path

import httpx
import pytest
import respx

from src.connectors.teamtailor import TeamtailorConnector
from src.models import ConnectorState

FIXTURE_RSS = (Path(__file__).parent.parent / "fixtures" / "teamtailor_example.rss").read_text()
URL = "https://tibber.teamtailor.com/jobs.rss"


@pytest.mark.asyncio
async def test_teamtailor_fetch_parses_items():
    conn = TeamtailorConnector("tibber")
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get(URL).respond(200, text=FIXTURE_RSS)
            result = await conn.fetch(client, ConnectorState())

    postings = result.postings
    assert len(postings) == 2
    p = postings[0]
    assert p.source == "teamtailor:tibber"
    assert p.external_id == "2b1eb6b1-25e0-47f9-bacb-f921ab07b2b5"
    assert p.title == "Senior AI Platform Engineer"
    assert p.apply_url == "https://jobs.tibber.com/jobs/7997072-senior-ai-platform-engineer"
    assert p.department == "Engineering"
    assert "Python" in p.description and "<strong>" not in p.description
    assert p.posted_at is not None and p.posted_at.year == 2026


@pytest.mark.asyncio
async def test_teamtailor_remote_status_and_locations():
    conn = TeamtailorConnector("tibber")
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get(URL).respond(200, text=FIXTURE_RSS)
            result = await conn.fetch(client, ConnectorState())

    hybrid_p, remote_p = result.postings
    # remoteStatus "hybrid" → remote False, "(Hybrid)" annotated onto the joined multi-location text
    assert hybrid_p.remote is False
    assert hybrid_p.location == "Amsterdam, Netherlands; Berlin, Germany (Hybrid)"
    # remoteStatus "fully" → remote True; empty locations → None
    assert remote_p.remote is True
    assert remote_p.location is None
    assert remote_p.posted_at is None  # no pubDate → UNKNOWN age path


@pytest.mark.asyncio
async def test_teamtailor_sends_if_none_match():
    conn = TeamtailorConnector("tibber")
    async with httpx.AsyncClient() as client:
        with respx.mock:
            route = respx.get(URL).respond(200, text=FIXTURE_RSS, headers={"ETag": 'W/"new"'})
            await conn.fetch(client, ConnectorState(etag='W/"abc"'))

    assert route.calls.last.request.headers.get("if-none-match") == 'W/"abc"'


@pytest.mark.asyncio
async def test_teamtailor_returns_not_modified_on_304():
    conn = TeamtailorConnector("tibber")
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get(URL).respond(304)
            result = await conn.fetch(client, ConnectorState(etag='W/"abc"'))

    assert result.not_modified is True
    assert result.postings == []
    assert result.new_state is None


@pytest.mark.asyncio
async def test_teamtailor_extracts_new_etag_on_200():
    conn = TeamtailorConnector("tibber")
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get(URL).respond(200, text=FIXTURE_RSS, headers={"ETag": 'W/"v2"'})
            result = await conn.fetch(client, ConnectorState())

    assert result.new_state is not None
    assert result.new_state.etag == 'W/"v2"'
