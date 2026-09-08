import json
from pathlib import Path

import httpx
import pytest
import respx

from src.connectors.greenhouse import GreenhouseConnector
from src.models import ConnectorState

FIXTURE = json.loads(
    (Path(__file__).parent.parent / "fixtures" / "greenhouse_example.json").read_text()
)


@pytest.mark.asyncio
async def test_greenhouse_fetch_parses_jobs():
    conn = GreenhouseConnector("stripe")
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get(
                "https://boards-api.greenhouse.io/v1/boards/stripe/jobs"
            ).respond(200, json=FIXTURE)
            result = await conn.fetch(client, ConnectorState())
            postings = result.postings

    assert len(postings) == 3
    p = postings[0]
    assert p.source == "greenhouse:stripe"
    assert p.external_id == "4567890"
    assert p.title == "Senior Backend Engineer"
    assert p.location == "Remote, United States, US"
    assert p.apply_url.endswith("/4567890")
    assert "Python and Go" in p.description
    assert "<" not in p.description  # HTML stripped
    assert p.posted_at is not None
    assert p.comp_min == 180_000
    assert p.comp_max == 240_000


@pytest.mark.asyncio
async def test_greenhouse_falls_back_to_offices_when_location_is_na():
    """Greenhouse often returns location.name='N/A' for roles whose real
    geography lives in the offices[] array. The connector must fold those
    office names into the location string so downstream filters can act on
    them (e.g. a Canada-only role gets tagged with a foreign country tag,
    country:ca, instead of unknown)."""
    conn = GreenhouseConnector("stripe")
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get(
                "https://boards-api.greenhouse.io/v1/boards/stripe/jobs"
            ).respond(200, json=FIXTURE)
            result = await conn.fetch(client, ConnectorState())
            postings = result.postings

    p = next(x for x in postings if x.external_id == "4567892")
    assert p.location == "Canada Locations"


@pytest.mark.asyncio
async def test_greenhouse_fetch_returns_empty_on_5xx():
    conn = GreenhouseConnector("stripe")
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get(
                "https://boards-api.greenhouse.io/v1/boards/stripe/jobs"
            ).respond(503)
            with pytest.raises(httpx.HTTPStatusError):
                await conn.fetch(client, ConnectorState())


@pytest.mark.asyncio
async def test_greenhouse_posted_at_uses_first_published_when_present():
    """Greenhouse exposes both first_published and updated_at; we must use the former
    to avoid alerting on stale postings that were merely re-edited."""
    conn = GreenhouseConnector("stripe")
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get(
                "https://boards-api.greenhouse.io/v1/boards/stripe/jobs"
            ).respond(200, json=FIXTURE)
            result = await conn.fetch(client, ConnectorState())
            postings = result.postings

    p = postings[0]  # Senior Backend Engineer
    assert p.posted_at is not None
    assert p.posted_at.year == 2026 and p.posted_at.month == 1, (
        f"expected first_published (Jan 2026), got {p.posted_at!r}"
    )


@pytest.mark.asyncio
async def test_greenhouse_posted_at_falls_back_to_updated_at_when_first_published_missing():
    conn = GreenhouseConnector("stripe")
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get(
                "https://boards-api.greenhouse.io/v1/boards/stripe/jobs"
            ).respond(200, json=FIXTURE)
            result = await conn.fetch(client, ConnectorState())
            postings = result.postings

    p = postings[1]  # Office Manager — fixture has no first_published
    assert p.posted_at is not None
    assert p.posted_at.month == 4, f"expected updated_at (Apr 2026), got {p.posted_at!r}"


@pytest.mark.asyncio
async def test_greenhouse_sends_if_none_match_when_etag_known():
    conn = GreenhouseConnector("stripe")
    captured: list[httpx.Request] = []
    async with httpx.AsyncClient() as client:
        with respx.mock:
            route = respx.get(
                "https://boards-api.greenhouse.io/v1/boards/stripe/jobs"
            ).respond(200, json=FIXTURE, headers={"ETag": 'W/"new"'})
            await conn.fetch(client, ConnectorState(etag='W/"abc"', last_modified=None))
            captured.append(route.calls.last.request)

    assert captured[0].headers.get("if-none-match") == 'W/"abc"'


@pytest.mark.asyncio
async def test_greenhouse_returns_not_modified_on_304():
    conn = GreenhouseConnector("stripe")
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get(
                "https://boards-api.greenhouse.io/v1/boards/stripe/jobs"
            ).respond(304)
            result = await conn.fetch(client, ConnectorState(etag='W/"abc"', last_modified=None))

    assert result.not_modified is True
    assert result.postings == []
    assert result.new_state is None  # leave persisted state unchanged


@pytest.mark.asyncio
async def test_greenhouse_extracts_new_etag_on_200():
    conn = GreenhouseConnector("stripe")
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get(
                "https://boards-api.greenhouse.io/v1/boards/stripe/jobs"
            ).respond(
                200,
                json=FIXTURE,
                headers={"ETag": 'W/"new"', "Last-Modified": "Wed, 30 Apr 2026 18:00:00 GMT"},
            )
            result = await conn.fetch(client, ConnectorState())

    assert result.not_modified is False
    assert result.new_state is not None
    assert result.new_state.etag == 'W/"new"'
    assert result.new_state.last_modified == "Wed, 30 Apr 2026 18:00:00 GMT"
