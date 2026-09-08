import json
from pathlib import Path

import httpx
import pytest
import respx

from src.models import ConnectorState
from src.sightings import Sighting

FIXTURE = json.loads(
    (Path(__file__).parent.parent / "fixtures" / "adzuna_example.json").read_text()
)

_SEARCH_US = "https://api.adzuna.com/v1/api/jobs/us/search/1"


def _conn(**overrides):
    from src.connectors.adzuna import AdzunaConnector

    kw = dict(
        app_id="test-id", app_key="test-key",
        countries=["us"], queries=["staff software engineer"],
        max_days_old=2, results_per_page=50, daily_call_budget=60,
        active_set=set(), sightings=None,
    )
    kw.update(overrides)
    return AdzunaConnector(**kw)


def _mock_search(payload=FIXTURE):
    """GET route for the us search endpoint + a catch-all HEAD (resolution
    no-ops to the requested URL until cycle C's explicit redirect mocks)."""
    route = respx.get(_SEARCH_US).respond(200, json=payload)
    respx.head(url__regex=r".*").respond(200)
    return route


@pytest.mark.asyncio
async def test_adzuna_name_and_tier():
    conn = _conn()
    assert conn.name == "adzuna"
    assert conn.tier == "slow"


@pytest.mark.asyncio
async def test_fetch_builds_authenticated_query():
    conn = _conn()
    async with httpx.AsyncClient() as client:
        with respx.mock:
            route = _mock_search()
            await conn.fetch(client, ConnectorState())
    params = route.calls.last.request.url.params
    assert params["app_id"] == "test-id"
    assert params["app_key"] == "test-key"
    assert params["what"] == "staff software engineer"
    assert params["results_per_page"] == "50"
    assert params["max_days_old"] == "2"
    assert params["sort_by"] == "date"


@pytest.mark.asyncio
async def test_fetch_parses_postings():
    conn = _conn()
    async with httpx.AsyncClient() as client:
        with respx.mock:
            _mock_search()
            result = await conn.fetch(client, ConnectorState())

    assert len(result.postings) == 2
    p = result.postings[0]
    assert p.source == "adzuna"
    assert p.external_id == "5001"
    assert p.title == "Staff Software Engineer"
    assert "<" not in p.description          # HTML stripped from the snippet
    assert p.company == "Acme Robotics"
    assert p.location == "Austin, TX"
    assert p.posted_at is not None and p.posted_at.year == 2026
    assert p.comp_min == 180000 and p.comp_max == 220000
    assert p.employment_type == "full_time"


@pytest.mark.asyncio
async def test_fetch_skips_predicted_salaries():
    conn = _conn()
    async with httpx.AsyncClient() as client:
        with respx.mock:
            _mock_search()
            result = await conn.fetch(client, ConnectorState())
    p = result.postings[1]
    assert p.external_id == "5002"
    assert p.comp_min is None and p.comp_max is None  # salary_is_predicted == "1"


@pytest.mark.asyncio
async def test_fetch_queries_each_country_and_query():
    conn = _conn(countries=["us", "gb"], queries=["staff engineer", "platform engineer"])
    async with httpx.AsyncClient() as client:
        with respx.mock:
            us = respx.get(_SEARCH_US).respond(200, json={"results": []})
            gb = respx.get("https://api.adzuna.com/v1/api/jobs/gb/search/1").respond(
                200, json={"results": []}
            )
            respx.head(url__regex=r".*").respond(200)
            await conn.fetch(client, ConnectorState())
    assert us.call_count == 2 and gb.call_count == 2


@pytest.mark.asyncio
async def test_postings_normalize_for_the_filter_pipeline():
    """Spec: normalize→filter integration — posted_at feeds filter_age,
    location_tags feed filter_location, employment_type is canonicalized."""
    from src.normalize import normalize

    conn = _conn()
    async with httpx.AsyncClient() as client:
        with respx.mock:
            _mock_search()
            result = await conn.fetch(client, ConnectorState())

    n = normalize(result.postings[0], stack_keywords=["python"], allowed_cities=["austin"])
    assert n.job_id == "adzuna:5001"
    assert n.company == "Acme Robotics"
    assert n.posted_at is not None                 # filter_age has a real timestamp
    assert "austin" in n.location_tags             # filter_location city tag
    assert n.employment_type == "full_time"        # canonical, not raw


@pytest.mark.asyncio
async def test_first_call_http_error_propagates_for_poll_health():
    conn = _conn()
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get(_SEARCH_US).respond(401)
            with pytest.raises(httpx.HTTPStatusError):
                await conn.fetch(client, ConnectorState())


@pytest.mark.asyncio
async def test_later_call_failure_keeps_partial_results():
    conn = _conn(queries=["q1", "q2"])
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get(_SEARCH_US).mock(
                side_effect=[
                    httpx.Response(200, json=FIXTURE),
                    httpx.Response(500),
                ]
            )
            respx.head(url__regex=r".*").respond(200)
            result = await conn.fetch(client, ConnectorState())
    assert len(result.postings) == 2  # first query's results survive


@pytest.mark.asyncio
async def test_non_json_200_keeps_partial_results_and_budget():
    conn = _conn(queries=["q1", "q2"])
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get(_SEARCH_US).mock(
                side_effect=[
                    httpx.Response(200, json=FIXTURE),
                    httpx.Response(200, text="<html>upstream error page</html>"),
                ]
            )
            respx.head(url__regex=r".*").respond(200)
            result = await conn.fetch(client, ConnectorState())
    assert len(result.postings) == 2                       # query 1's results survive
    assert result.new_state.payload["budget_calls"] == 2   # both calls counted


@pytest.mark.asyncio
async def test_budget_guard_stops_api_calls():
    conn = _conn(queries=["q1", "q2", "q3"], daily_call_budget=1)
    async with httpx.AsyncClient() as client:
        with respx.mock:
            route = _mock_search()
            result = await conn.fetch(client, ConnectorState())
    assert route.call_count == 1                       # hard stop after the budget
    assert result.new_state.payload["budget_calls"] == 1
    assert len(result.postings) == 2                   # partial results still emitted


@pytest.mark.asyncio
async def test_budget_resets_on_new_day():
    conn = _conn()
    stale = ConnectorState(
        payload={"budget_date": "2020-01-01", "budget_calls": 999, "seen_ids": []}
    )
    async with httpx.AsyncClient() as client:
        with respx.mock:
            route = _mock_search()
            result = await conn.fetch(client, stale)
    assert route.call_count == 1                       # stale counter didn't block
    assert result.new_state.payload["budget_calls"] == 1
    assert result.new_state.payload["budget_date"] != "2020-01-01"


@pytest.mark.asyncio
async def test_seen_ids_skip_reprocessing():
    conn = _conn()
    prior = ConnectorState(payload={"seen_ids": ["5001"]})
    async with httpx.AsyncClient() as client:
        with respx.mock:
            _mock_search()
            result = await conn.fetch(client, prior)
    assert [p.external_id for p in result.postings] == ["5002"]
    assert set(result.new_state.payload["seen_ids"]) == {"5001", "5002"}


@pytest.mark.asyncio
async def test_seen_ids_fifo_cap():
    from src.connectors.adzuna import _SEEN_CAP

    conn = _conn()
    prior = ConnectorState(
        payload={"seen_ids": [f"old{i}" for i in range(_SEEN_CAP)]}
    )
    async with httpx.AsyncClient() as client:
        with respx.mock:
            _mock_search()
            result = await conn.fetch(client, prior)
    ids = result.new_state.payload["seen_ids"]
    assert len(ids) == _SEEN_CAP
    assert ids[-1] == "5002" and "old0" not in ids     # oldest evicted


_GH_LAND = "https://www.adzuna.com/land/ad/5001?se=abc"
_GH_FINAL = "https://boards.greenhouse.io/acmerobotics/jobs/123"
_OFFATS_LAND = "https://www.adzuna.com/land/ad/5002?se=def"
_OFFATS_FINAL = "https://talentbridge.example.com/jobs/42"


def _mock_redirects():
    respx.head(_GH_LAND).respond(302, headers={"Location": _GH_FINAL})
    respx.head(_GH_FINAL).respond(200)
    respx.head(_OFFATS_LAND).respond(302, headers={"Location": _OFFATS_FINAL})
    respx.head(_OFFATS_FINAL).respond(200)


@pytest.mark.asyncio
async def test_resolution_rewrites_apply_url_and_emits_sightings():
    sightings: list[Sighting] = []
    conn = _conn(sightings=sightings)
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get(_SEARCH_US).respond(200, json=FIXTURE)
            _mock_redirects()
            result = await conn.fetch(client, ConnectorState())

    urls = {p.external_id: p.apply_url for p in result.postings}
    assert urls == {"5001": _GH_FINAL, "5002": _OFFATS_FINAL}
    assert len(sightings) == 2                          # ATS-hosted AND off-ATS both sighted
    assert all(s.origin == "adzuna" for s in sightings)
    assert sightings[0].apply_url == _GH_FINAL


@pytest.mark.asyncio
async def test_active_set_suppresses_directly_polled_boards():
    sightings: list[Sighting] = []
    conn = _conn(active_set={("greenhouse", "acmerobotics")}, sightings=sightings)
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get(_SEARCH_US).respond(200, json=FIXTURE)
            _mock_redirects()
            result = await conn.fetch(client, ConnectorState())

    assert [p.external_id for p in result.postings] == ["5002"]   # 5001 suppressed
    assert [s.apply_url for s in sightings] == [_OFFATS_FINAL]    # no sighting either
    assert "5001" in result.new_state.payload["seen_ids"]         # but still marked seen


@pytest.mark.asyncio
async def test_resolution_failure_falls_back_to_redirect_url():
    conn = _conn()
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get(_SEARCH_US).respond(200, json=FIXTURE)
            respx.head(url__regex=r".*").mock(side_effect=httpx.ConnectError("boom"))
            respx.route(method="GET", url__regex=r"https://www\.adzuna\.com/land/.*").mock(
                side_effect=httpx.ConnectError("boom")
            )
            result = await conn.fetch(client, ConnectorState())

    assert len(result.postings) == 2                    # fail open
    assert result.postings[0].apply_url == _GH_LAND     # adzuna link retained
    assert "5001" in result.new_state.payload["seen_ids"]  # no re-resolution next cycle
