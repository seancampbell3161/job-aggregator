import json
from pathlib import Path

import httpx
import pytest
import respx

from src.connectors.rippling import RipplingConnector
from src.models import ConnectorState

FIXTURE = json.loads(
    (Path(__file__).parent.parent / "fixtures" / "rippling_example.json").read_text()
)
URL = "https://api.rippling.com/platform/api/ats/v1/board/acme/jobs"


@pytest.mark.asyncio
async def test_rippling_fetch_parses_jobs():
    conn = RipplingConnector("acme")
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get(URL).respond(200, json=FIXTURE)
            result = await conn.fetch(client, ConnectorState())
    postings = result.postings
    assert len(postings) == 4
    p = postings[0]
    assert p.source == "rippling:acme"
    assert p.external_id == "u1"
    assert p.title == "Senior Backend Engineer"
    assert p.location == "Remote (United States)"
    assert p.department == "Engineering"
    assert p.apply_url == "https://ats.rippling.com/acme/jobs/u1"
    assert p.description == ""
    assert p.comp_min is None and p.comp_max is None
    assert p.posted_at is None


@pytest.mark.asyncio
async def test_rippling_non_list_body_yields_empty():
    conn = RipplingConnector("acme")
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get(URL).respond(200, json={"error": "nope"})
            result = await conn.fetch(client, ConnectorState())
    assert result.postings == []


@pytest.mark.asyncio
async def test_rippling_handles_missing_fields_and_skips_uuidless():
    """A job missing workLocation/department must not crash (location/department
    None); a job with no uuid is skipped (an empty external_id would corrupt the
    job_id used for dedup)."""
    body = [
        {"uuid": "ok1", "name": "Engineer", "url": "https://x/ok1"},  # no workLocation/department
        {"name": "Ghost", "url": "https://x/ghost"},                  # no uuid → skipped
    ]
    conn = RipplingConnector("acme")
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get(URL).respond(200, json=body)
            result = await conn.fetch(client, ConnectorState())
    assert [p.external_id for p in result.postings] == ["ok1"]
    assert result.postings[0].location is None
    assert result.postings[0].department is None


@pytest.mark.asyncio
async def test_rippling_raises_on_5xx():
    conn = RipplingConnector("acme")
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get(URL).respond(503)
            with pytest.raises(httpx.HTTPStatusError):
                await conn.fetch(client, ConnectorState())


@pytest.mark.asyncio
async def test_rippling_304_is_not_modified():
    conn = RipplingConnector("acme")
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get(URL).respond(304)
            result = await conn.fetch(client, ConnectorState(etag='W/"x"', last_modified=None))
    assert result.not_modified is True
    assert result.postings == []
    assert result.new_state is None


@pytest.mark.asyncio
async def test_rippling_sends_if_none_match_and_extracts_etag():
    conn = RipplingConnector("acme")
    async with httpx.AsyncClient() as client:
        with respx.mock:
            route = respx.get(URL).respond(200, json=FIXTURE, headers={"ETag": 'W/"new"'})
            result = await conn.fetch(client, ConnectorState(etag='W/"old"', last_modified=None))
            assert route.calls.last.request.headers.get("if-none-match") == 'W/"old"'
    assert result.new_state is not None
    assert result.new_state.etag == 'W/"new"'
