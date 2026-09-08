from pathlib import Path

import httpx
import pytest
import respx

from src.connectors.personio import PersonioConnector
from src.models import ConnectorState

FIXTURE_XML = (Path(__file__).parent.parent / "fixtures" / "personio_example.xml").read_text()
URL_DE = "https://everphone.jobs.personio.de/xml"
URL_COM = "https://everphone.jobs.personio.com/xml"


@pytest.mark.asyncio
async def test_personio_fetch_parses_positions():
    conn = PersonioConnector("everphone")
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get(URL_DE).respond(200, text=FIXTURE_XML)
            result = await conn.fetch(client, ConnectorState())

    postings = result.postings
    assert len(postings) == 2
    p = postings[0]
    assert p.source == "personio:everphone"
    assert p.external_id == "1869590"
    assert p.title == "Senior Backend Engineer (m/w/d)"
    assert p.company == "Everphone GmbH"
    assert p.location == "Berlin"
    assert p.department == "Engineering"
    assert p.apply_url == "https://everphone.jobs.personio.de/job/1869590"
    assert "Python" in p.description and "<strong>" not in p.description
    assert p.posted_at is not None and p.posted_at.year == 2024


@pytest.mark.asyncio
async def test_personio_dateless_position_and_empty_subcompany():
    conn = PersonioConnector("everphone")
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get(URL_DE).respond(200, text=FIXTURE_XML)
            result = await conn.fetch(client, ConnectorState())

    p = result.postings[1]
    assert p.posted_at is None       # no createdAt → rides filter_age's UNKNOWN path
    assert p.company is None         # empty subcompany element
    assert p.location == "Remote"    # normalizer's remote regex handles this
    assert p.description == ""       # empty jobDescriptions is legal


@pytest.mark.asyncio
async def test_personio_falls_back_to_com_host():
    # Unknown-on-.de tenants 307-redirect to the personio.com marketing site;
    # a real .com tenant serves 200 there. Only direct 200/304 counts.
    conn = PersonioConnector("everphone")
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get(URL_DE).respond(307, headers={"location": "https://personio.com"})
            respx.get(URL_COM).respond(200, text=FIXTURE_XML)
            result = await conn.fetch(client, ConnectorState())

    assert len(result.postings) == 2
    # apply URLs are built on the host that actually served the feed
    assert result.postings[0].apply_url == "https://everphone.jobs.personio.com/job/1869590"


@pytest.mark.asyncio
async def test_personio_raises_when_both_hosts_fail():
    conn = PersonioConnector("nosuchtenant")
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get("https://nosuchtenant.jobs.personio.de/xml").respond(404)
            respx.get("https://nosuchtenant.jobs.personio.com/xml").respond(404)
            with pytest.raises(httpx.HTTPStatusError):
                await conn.fetch(client, ConnectorState())


@pytest.mark.asyncio
async def test_personio_returns_not_modified_on_304():
    conn = PersonioConnector("everphone")
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get(URL_DE).respond(304)
            result = await conn.fetch(client, ConnectorState(etag='W/"abc"'))

    assert result.not_modified is True
    assert result.postings == []


@pytest.mark.asyncio
async def test_personio_sends_if_none_match_and_extracts_etag():
    conn = PersonioConnector("everphone")
    async with httpx.AsyncClient() as client:
        with respx.mock:
            route = respx.get(URL_DE).respond(200, text=FIXTURE_XML, headers={"ETag": 'W/"v2"'})
            result = await conn.fetch(client, ConnectorState(etag='W/"abc"'))

    assert route.calls.last.request.headers.get("if-none-match") == 'W/"abc"'
    assert result.new_state is not None and result.new_state.etag == 'W/"v2"'
