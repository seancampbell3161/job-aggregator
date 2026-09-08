import json
from pathlib import Path

import httpx
import pytest
import respx

from src.connectors.recruitee import RecruiteeConnector
from src.models import ConnectorState

FIXTURE = json.loads(
    (Path(__file__).parent.parent / "fixtures" / "recruitee_example.json").read_text()
)
URL = "https://sendcloud.recruitee.com/api/offers/"


@pytest.mark.asyncio
async def test_recruitee_fetch_parses_postings():
    conn = RecruiteeConnector("sendcloud")
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get(URL).respond(200, json=FIXTURE)
            result = await conn.fetch(client, ConnectorState())

    postings = result.postings
    assert len(postings) == 2
    p = postings[0]
    assert p.source == "recruitee:sendcloud"
    assert p.external_id == "2429738"
    assert p.title == "Senior Backend Engineer"
    assert p.company == "Sendcloud"
    assert p.apply_url == "https://sendcloud.recruitee.com/o/senior-backend-engineer"
    assert "Python" in p.description and "<strong>" not in p.description
    assert p.posted_at is not None and p.posted_at.year == 2025  # published_at wins


def _fetch(fixture):
    async def go():
        conn = RecruiteeConnector("sendcloud")
        async with httpx.AsyncClient() as client:
            with respx.mock:
                respx.get(URL).respond(200, json=fixture)
                return await conn.fetch(client, ConnectorState())
    return go


@pytest.mark.asyncio
async def test_recruitee_workplace_booleans_map_to_remote_flag_and_location():
    result = await _fetch(FIXTURE)()
    hybrid_p, remote_p = result.postings
    # hybrid=true → remote False, "(Hybrid)" folded into the structured location
    assert hybrid_p.remote is False
    assert hybrid_p.location == "Amsterdam, Netherlands (Hybrid)"
    # remote=true → remote True; no city/country → location None
    assert remote_p.remote is True
    assert remote_p.location is None
    # published_at null → falls back to created_at
    assert remote_p.posted_at is not None and remote_p.posted_at.year == 2026


@pytest.mark.asyncio
async def test_recruitee_sends_if_none_match():
    conn = RecruiteeConnector("sendcloud")
    async with httpx.AsyncClient() as client:
        with respx.mock:
            route = respx.get(URL).respond(200, json=FIXTURE, headers={"ETag": 'W/"new"'})
            await conn.fetch(client, ConnectorState(etag='W/"abc"'))

    assert route.calls.last.request.headers.get("if-none-match") == 'W/"abc"'


@pytest.mark.asyncio
async def test_recruitee_returns_not_modified_on_304():
    conn = RecruiteeConnector("sendcloud")
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get(URL).respond(304)
            result = await conn.fetch(client, ConnectorState(etag='W/"abc"'))

    assert result.not_modified is True
    assert result.postings == []
    assert result.new_state is None


@pytest.mark.asyncio
async def test_recruitee_extracts_new_etag_on_200():
    conn = RecruiteeConnector("sendcloud")
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get(URL).respond(200, json=FIXTURE, headers={"ETag": 'W/"v2"'})
            result = await conn.fetch(client, ConnectorState())

    assert result.new_state is not None
    assert result.new_state.etag == 'W/"v2"'
