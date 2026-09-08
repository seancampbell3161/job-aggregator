import json
from pathlib import Path

import httpx
import pytest
import respx

from src.connectors.lever import LeverConnector
from src.models import ConnectorState

FIXTURE = json.loads(
    (Path(__file__).parent.parent / "fixtures" / "lever_example.json").read_text()
)


@pytest.mark.asyncio
async def test_lever_fetch_parses_jobs():
    conn = LeverConnector("example")
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get("https://api.lever.co/v0/postings/example").respond(200, json=FIXTURE)
            result = await conn.fetch(client, ConnectorState())
            postings = result.postings

    assert len(postings) == 1
    p = postings[0]
    assert p.source == "lever:example"
    assert p.external_id == "abc-123"
    assert p.title == "Software Engineer, Backend"
    assert p.location == "Remote - US"
    assert p.apply_url.endswith("abc-123")
    assert "Go" in p.description
    assert p.comp_min == 150_000
    assert p.comp_max == 200_000
    assert p.posted_at is not None
    # Raw commitment flows through; canonicalization happens in normalize().
    assert p.employment_type == "Full-time"


@pytest.mark.asyncio
async def test_lever_sends_if_none_match():
    conn = LeverConnector("example")
    async with httpx.AsyncClient() as client:
        with respx.mock:
            route = respx.get("https://api.lever.co/v0/postings/example").respond(
                200, json=FIXTURE, headers={"ETag": 'W/"new"'}
            )
            await conn.fetch(client, ConnectorState(etag='W/"abc"'))

    assert route.calls.last.request.headers.get("if-none-match") == 'W/"abc"'


@pytest.mark.asyncio
async def test_lever_returns_not_modified_on_304():
    conn = LeverConnector("example")
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get("https://api.lever.co/v0/postings/example").respond(304)
            result = await conn.fetch(client, ConnectorState(etag='W/"abc"'))

    assert result.not_modified is True
    assert result.postings == []
    assert result.new_state is None


@pytest.mark.asyncio
async def test_lever_extracts_new_etag_on_200():
    conn = LeverConnector("example")
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get("https://api.lever.co/v0/postings/example").respond(
                200, json=FIXTURE, headers={"ETag": 'W/"v2"'}
            )
            result = await conn.fetch(client, ConnectorState())

    assert result.new_state is not None
    assert result.new_state.etag == 'W/"v2"'


@pytest.mark.asyncio
async def test_lever_uses_workplace_type_for_remote_flag():
    """Lever exposes workplaceType (remote/hybrid/onsite); the connector uses it.
    Hybrid/onsite are NOT remote; absent → None (fall back to location text)."""
    jobs = [
        {"id": "1", "text": "SWE", "categories": {"location": "San Francisco"},
         "workplaceType": "remote", "hostedUrl": "https://x/1"},
        {"id": "2", "text": "SWE", "categories": {"location": "San Francisco"},
         "workplaceType": "hybrid", "hostedUrl": "https://x/2"},
        {"id": "3", "text": "SWE", "categories": {"location": "San Francisco"},
         "workplaceType": "onsite", "hostedUrl": "https://x/3"},
        {"id": "4", "text": "SWE", "categories": {"location": "San Francisco"},
         "hostedUrl": "https://x/4"},  # no workplaceType
    ]
    conn = LeverConnector("example")
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get("https://api.lever.co/v0/postings/example").respond(200, json=jobs)
            result = await conn.fetch(client, ConnectorState())

    by_id = {p.external_id: p for p in result.postings}
    assert {k: v.remote for k, v in by_id.items()} == {"1": True, "2": False, "3": False, "4": None}
    # Hybrid/onsite folded into the location text; remote/absent untouched.
    assert by_id["1"].location == "San Francisco"            # remote not annotated
    assert by_id["2"].location == "San Francisco (Hybrid)"
    assert by_id["3"].location == "San Francisco (Onsite)"
    assert by_id["4"].location == "San Francisco"            # no workplaceType
