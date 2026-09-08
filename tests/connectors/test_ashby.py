import json
from pathlib import Path

import httpx
import pytest
import respx

from src.connectors.ashby import AshbyConnector
from src.models import ConnectorState

FIXTURE = json.loads(
    (Path(__file__).parent.parent / "fixtures" / "ashby_example.json").read_text()
)


@pytest.mark.asyncio
async def test_ashby_fetch_parses_jobs():
    conn = AshbyConnector("example")
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get(
                "https://api.ashbyhq.com/posting-api/job-board/example"
            ).respond(200, json=FIXTURE)
            result = await conn.fetch(client, ConnectorState())
            postings = result.postings

    assert len(postings) == 1
    p = postings[0]
    assert p.source == "ashby:example"
    assert p.external_id == "ashby-1"
    assert p.title == "Senior Software Engineer"
    assert p.location == "Remote (US)"
    assert p.remote is True
    assert p.comp_min == 170_000
    assert p.comp_max == 220_000
    assert "Python" in p.description


@pytest.mark.asyncio
async def test_ashby_sends_if_none_match():
    conn = AshbyConnector("example")
    async with httpx.AsyncClient() as client:
        with respx.mock:
            route = respx.get(
                "https://api.ashbyhq.com/posting-api/job-board/example"
            ).respond(200, json=FIXTURE, headers={"ETag": 'W/"new"'})
            await conn.fetch(client, ConnectorState(etag='W/"abc"'))

    assert route.calls.last.request.headers.get("if-none-match") == 'W/"abc"'


@pytest.mark.asyncio
async def test_ashby_returns_not_modified_on_304():
    conn = AshbyConnector("example")
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get(
                "https://api.ashbyhq.com/posting-api/job-board/example"
            ).respond(304)
            result = await conn.fetch(client, ConnectorState(etag='W/"abc"'))

    assert result.not_modified is True
    assert result.postings == []
    assert result.new_state is None


@pytest.mark.asyncio
async def test_ashby_extracts_new_etag_on_200():
    conn = AshbyConnector("example")
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get(
                "https://api.ashbyhq.com/posting-api/job-board/example"
            ).respond(200, json=FIXTURE, headers={"ETag": 'W/"v2"'})
            result = await conn.fetch(client, ConnectorState())

    assert result.new_state is not None
    assert result.new_state.etag == 'W/"v2"'


@pytest.mark.asyncio
async def test_ashby_honors_workplace_type_over_isremote():
    """Ashby companies routinely leave isRemote=true on Hybrid/OnSite roles; the
    explicit workplaceType must win so they aren't mislabeled remote. When
    workplaceType is absent, fall back to isRemote."""
    jobs = [
        {"id": "h", "title": "SWE", "location": "San Francisco", "isRemote": True,
         "workplaceType": "Hybrid", "descriptionPlain": "x", "jobUrl": "https://x/h"},
        {"id": "o", "title": "SWE", "location": "San Francisco", "isRemote": True,
         "workplaceType": "OnSite", "descriptionPlain": "x", "jobUrl": "https://x/o"},
        {"id": "r", "title": "SWE", "location": "Remote", "isRemote": True,
         "workplaceType": "Remote", "descriptionPlain": "x", "jobUrl": "https://x/r"},
        {"id": "f", "title": "SWE", "location": "NYC", "isRemote": True,
         "descriptionPlain": "x", "jobUrl": "https://x/f"},  # no workplaceType → fallback
    ]
    conn = AshbyConnector("example")
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get(
                "https://api.ashbyhq.com/posting-api/job-board/example"
            ).respond(200, json={"jobs": jobs})
            result = await conn.fetch(client, ConnectorState())

    by_id = {p.external_id: p for p in result.postings}
    assert {k: v.remote for k, v in by_id.items()} == {"h": False, "o": False, "r": True, "f": True}
    # Hybrid/OnSite are folded into the location text so they tag distinctly
    # (a bare city would otherwise normalize to onsite_only).
    assert by_id["h"].location == "San Francisco (Hybrid)"
    assert by_id["o"].location == "San Francisco (Onsite)"
    assert by_id["r"].location == "Remote"     # remote not annotated
    assert by_id["f"].location == "NYC"         # no workplaceType → untouched


@pytest.mark.asyncio
async def test_ashby_explicit_null_workplace_type_falls_back_to_isremote():
    """An explicit ``workplaceType: null`` (not just an absent key) falls back to
    isRemote: True → remote, False → not remote. When both are null, remote is
    None and the location text decides downstream. ~38% of some boards (e.g.
    OpenAI) ship a null workplaceType, so this path matters."""
    jobs = [
        {"id": "n1", "title": "SWE", "location": "Austin", "workplaceType": None,
         "isRemote": True, "descriptionPlain": "x", "jobUrl": "https://x/n1"},
        {"id": "n2", "title": "SWE", "location": "Austin", "workplaceType": None,
         "isRemote": False, "descriptionPlain": "x", "jobUrl": "https://x/n2"},
        {"id": "n3", "title": "SWE", "location": "Austin", "workplaceType": None,
         "isRemote": None, "descriptionPlain": "x", "jobUrl": "https://x/n3"},
    ]
    conn = AshbyConnector("example")
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get(
                "https://api.ashbyhq.com/posting-api/job-board/example"
            ).respond(200, json={"jobs": jobs})
            result = await conn.fetch(client, ConnectorState())

    remotes = {p.external_id: p.remote for p in result.postings}
    assert remotes == {"n1": True, "n2": False, "n3": None}
