import json
from pathlib import Path

import httpx
import pytest
import respx

from src.connectors.remotive import RemotiveConnector
from src.models import ConnectorState

FIXTURE = json.loads(
    (Path(__file__).parent.parent / "fixtures" / "remotive_example.json").read_text()
)


@pytest.mark.asyncio
async def test_remotive_fetch_parses_jobs():
    conn = RemotiveConnector()
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get("https://remotive.com/api/remote-jobs").respond(200, json=FIXTURE)
            result = await conn.fetch(client, ConnectorState())
            postings = result.postings

    assert len(postings) == 2

    p = postings[0]
    assert p.source == "remotive"
    assert p.external_id == "1234567"
    assert p.title == "Senior Backend Engineer"
    assert p.apply_url == "https://remotive.com/remote-jobs/software-dev/senior-backend-engineer-1234567"
    assert p.location == "USA, Canada"
    assert p.remote is True
    assert p.comp_min == 150_000
    assert p.comp_max == 200_000
    assert "<" not in p.description  # HTML stripped
    assert "Senior Backend Engineer" in p.description
    assert "Tags:" in p.description
    assert "python" in p.description


@pytest.mark.asyncio
async def test_remotive_fetch_no_salary_leaves_comp_none():
    conn = RemotiveConnector()
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get("https://remotive.com/api/remote-jobs").respond(200, json=FIXTURE)
            result = await conn.fetch(client, ConnectorState())
            postings = result.postings

    p = postings[1]
    assert p.source == "remotive"
    assert p.external_id == "7654321"
    assert p.comp_min is None
    assert p.comp_max is None
    assert p.remote is True


@pytest.mark.asyncio
async def test_remotive_connector_name_and_tier():
    conn = RemotiveConnector()
    assert conn.name == "remotive"
    assert conn.tier == "slow"


@pytest.mark.asyncio
async def test_remotive_fetch_raises_on_5xx():
    conn = RemotiveConnector()
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get("https://remotive.com/api/remote-jobs").respond(503)
            with pytest.raises(httpx.HTTPStatusError):
                await conn.fetch(client, ConnectorState())
