import json
from pathlib import Path

import httpx
import pytest
import respx

from src.connectors.remoteok import RemoteOkConnector
from src.models import ConnectorState

FIXTURE = json.loads(
    (Path(__file__).parent.parent / "fixtures" / "remoteok_example.json").read_text()
)


@pytest.mark.asyncio
async def test_remoteok_fetch_parses_jobs():
    conn = RemoteOkConnector()
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get("https://remoteok.com/api").respond(200, json=FIXTURE)
            result = await conn.fetch(client, ConnectorState())
            postings = result.postings

    # First element (metadata/legal) is skipped
    assert len(postings) == 2

    p = postings[0]
    assert p.source == "remoteok"
    assert p.external_id == "9001"
    assert p.title == "Senior Backend Engineer"
    assert p.apply_url == "https://remoteOK.com/remote-jobs/remote-senior-backend-engineer-acme-corp-9001"
    assert p.location == "Worldwide"
    assert p.remote is True
    assert p.comp_min == 150_000
    assert p.comp_max == 200_000
    assert "<" not in p.description  # HTML stripped
    assert "Senior Backend Engineer" in p.description
    assert "Tags:" in p.description
    assert "python" in p.description
    assert p.posted_at is not None


@pytest.mark.asyncio
async def test_remoteok_fetch_zero_salary_leaves_comp_none():
    conn = RemoteOkConnector()
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get("https://remoteok.com/api").respond(200, json=FIXTURE)
            result = await conn.fetch(client, ConnectorState())
            postings = result.postings

    p = postings[1]
    assert p.source == "remoteok"
    assert p.external_id == "9002"
    # salary_min/max are 0 in fixture — should be treated as None
    assert p.comp_min is None
    assert p.comp_max is None
    assert p.remote is True


@pytest.mark.asyncio
async def test_remoteok_connector_name_and_tier():
    conn = RemoteOkConnector()
    assert conn.name == "remoteok"
    assert conn.tier == "slow"


@pytest.mark.asyncio
async def test_remoteok_fetch_raises_on_5xx():
    conn = RemoteOkConnector()
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get("https://remoteok.com/api").respond(503)
            with pytest.raises(httpx.HTTPStatusError):
                await conn.fetch(client, ConnectorState())
