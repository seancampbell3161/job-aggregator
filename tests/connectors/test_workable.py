import json
from pathlib import Path

import httpx
import pytest
import respx

from src.connectors.workable import WorkableConnector
from src.models import ConnectorState

FIXTURE = json.loads(
    (Path(__file__).parent.parent / "fixtures" / "workable_example.json").read_text()
)


@pytest.mark.asyncio
async def test_workable_fetch_parses_jobs():
    conn = WorkableConnector("example")
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.post(
                "https://apply.workable.com/api/v3/accounts/example/jobs"
            ).respond(200, json=FIXTURE)
            result = await conn.fetch(client, ConnectorState())
            postings = result.postings

    assert len(postings) == 1
    p = postings[0]
    assert p.source == "workable:example"
    assert p.external_id == "ABCD1234"  # shortcode preferred — stable in URL
    assert p.title == "Backend Engineer"
    assert "Remote" in (p.location or "")
    assert p.remote is True
    assert "Python" in p.description
    assert "<" not in p.description


@pytest.mark.asyncio
async def test_workable_sends_if_none_match():
    conn = WorkableConnector("example")
    async with httpx.AsyncClient() as client:
        with respx.mock:
            route = respx.post(
                "https://apply.workable.com/api/v3/accounts/example/jobs"
            ).respond(200, json=FIXTURE, headers={"ETag": 'W/"new"'})
            await conn.fetch(client, ConnectorState(etag='W/"abc"'))

    assert route.calls.last.request.headers.get("if-none-match") == 'W/"abc"'


@pytest.mark.asyncio
async def test_workable_returns_not_modified_on_304():
    conn = WorkableConnector("example")
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.post(
                "https://apply.workable.com/api/v3/accounts/example/jobs"
            ).respond(304)
            result = await conn.fetch(client, ConnectorState(etag='W/"abc"'))

    assert result.not_modified is True
    assert result.postings == []
    assert result.new_state is None


@pytest.mark.asyncio
async def test_workable_extracts_new_etag_on_200():
    conn = WorkableConnector("example")
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.post(
                "https://apply.workable.com/api/v3/accounts/example/jobs"
            ).respond(200, json=FIXTURE, headers={"ETag": 'W/"v2"'})
            result = await conn.fetch(client, ConnectorState())

    assert result.new_state is not None
    assert result.new_state.etag == 'W/"v2"'
