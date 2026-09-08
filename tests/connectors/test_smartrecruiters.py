import json
from pathlib import Path

import httpx
import pytest
import respx

from src.connectors.smartrecruiters import SmartRecruitersConnector
from src.models import ConnectorState

FIXTURE = json.loads(
    (Path(__file__).parent.parent / "fixtures" / "smartrecruiters_example.json").read_text()
)


@pytest.mark.asyncio
async def test_smartrecruiters_fetch_parses_postings():
    conn = SmartRecruitersConnector("deliveryhero")
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get(
                "https://api.smartrecruiters.com/v1/companies/deliveryhero/postings"
            ).respond(200, json=FIXTURE)
            result = await conn.fetch(client, ConnectorState())

    postings = result.postings
    assert len(postings) >= 1
    p = postings[0]
    assert p.source == "smartrecruiters:deliveryhero"
    assert p.external_id  # non-empty
    assert p.title  # non-empty
    assert p.apply_url.startswith("http")


@pytest.mark.asyncio
async def test_smartrecruiters_sends_if_none_match():
    conn = SmartRecruitersConnector("deliveryhero")
    async with httpx.AsyncClient() as client:
        with respx.mock:
            route = respx.get(
                "https://api.smartrecruiters.com/v1/companies/deliveryhero/postings"
            ).respond(200, json=FIXTURE, headers={"ETag": 'W/"new"'})
            await conn.fetch(client, ConnectorState(etag='W/"abc"'))

    assert route.calls.last.request.headers.get("if-none-match") == 'W/"abc"'


@pytest.mark.asyncio
async def test_smartrecruiters_returns_not_modified_on_304():
    conn = SmartRecruitersConnector("deliveryhero")
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get(
                "https://api.smartrecruiters.com/v1/companies/deliveryhero/postings"
            ).respond(304)
            result = await conn.fetch(client, ConnectorState(etag='W/"abc"'))

    assert result.not_modified is True
    assert result.postings == []
    assert result.new_state is None


@pytest.mark.asyncio
async def test_smartrecruiters_extracts_new_etag_on_200():
    conn = SmartRecruitersConnector("deliveryhero")
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get(
                "https://api.smartrecruiters.com/v1/companies/deliveryhero/postings"
            ).respond(200, json=FIXTURE, headers={"ETag": 'W/"v2"'})
            result = await conn.fetch(client, ConnectorState())

    assert result.new_state is not None
    assert result.new_state.etag == 'W/"v2"'
