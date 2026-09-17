import json
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from src.hiringcafe import HiringCafeClient, parse_jobs

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "hiringcafe_example.json").read_text()
)


def test_parse_jobs_returns_empty_on_unrecognized_shape():
    """Defensive parser: an unexpected schema does not crash the pipeline."""
    assert parse_jobs({"unexpected": "shape"}) == []
    assert parse_jobs({}) == []
    assert parse_jobs(None) == []  # type: ignore[arg-type]


def test_parse_jobs_extracts_known_fields_from_fixture():
    jobs = parse_jobs(FIXTURE)
    assert len(jobs) >= 1
    j = jobs[0]
    assert j.title  # non-empty
    assert j.apply_url.startswith("http")
    # ats_family / ats_slug come from `source` and `board_token`
    assert j.ats_family is not None
    assert j.ats_slug is not None
    assert j.external_id is not None


def test_parse_jobs_extracts_commitment_as_employment_type():
    """v5_processed_job_data.commitment (a list) → the raw employment_type string,
    which normalize() later canonicalizes. The fixture's jobs are Full Time."""
    jobs = parse_jobs(FIXTURE)
    with_commit = [j for j in jobs if j.employment_type]
    assert with_commit, "expected at least one fixture job to carry a commitment"
    assert with_commit[0].employment_type == "Full Time"


def test_parse_jobs_reads_contract_commitment():
    jobs = parse_jobs({
        "pageProps": {
            "ssrHits": [
                {
                    "id": "c-1",
                    "board_token": "acme",
                    "source": "greenhouse",
                    "apply_url": "https://example.com/apply",
                    "job_information": {"title": "Contractor SWE"},
                    "v5_processed_job_data": {"commitment": ["Contract"]},
                },
            ]
        }
    })
    assert len(jobs) == 1
    assert jobs[0].employment_type == "Contract"


def test_parse_jobs_yields_a_supported_ats_among_results():
    """Among the hits in the fixture, at least one should be on a supported ATS
    (greenhouse, lever, ashby, workable, smartrecruiters). If not, our discovery
    coverage from this page would be 0 — sanity check the page."""
    jobs = parse_jobs(FIXTURE)
    supported = {"greenhouse", "lever", "ashby", "workable", "smartrecruiters"}
    found = {j.ats_family for j in jobs if j.ats_family in supported}
    assert found, f"no supported ATS in fixture; found families: {sorted({j.ats_family for j in jobs if j.ats_family})[:10]}"


def test_parse_jobs_skips_entries_with_missing_required_fields():
    """A posting without title or apply_url is skipped silently."""
    jobs = parse_jobs({
        "pageProps": {
            "ssrHits": [
                {"board_token": "x", "source": "greenhouse"},  # no title, no url
                {
                    "id": "ok-1",
                    "board_token": "good",
                    "source": "greenhouse",
                    "apply_url": "https://example.com/apply",
                    "job_information": {"title": "SWE"},
                },
            ]
        }
    })
    assert len(jobs) == 1
    assert jobs[0].ats_slug == "good"


@pytest.mark.asyncio
async def test_client_search_posts_to_endpoint():
    """HiringCafeClient.search() returns the parsed JSON response."""
    async with httpx.AsyncClient() as client:
        with respx.mock:
            # The client uses the Next.js _next/data path that the SSR pageProps
            # response was captured from. Match any request under that prefix.
            respx.route(method="GET", url__regex=r"https://hiringcafe\.com/_next/data/.*").respond(
                200, json=FIXTURE
            )
            # buildId comes from /jobs, not the homepage: the homepage sits
            # behind a Cloudflare challenge (see hiringcafe._BUILD_ID_SOURCE).
            respx.route(method="GET", url="https://hiringcafe.com/jobs").respond(
                200,
                text='<html><script id="__NEXT_DATA__">{"buildId":"test-build-id-123"}</script></html>',
            )
            cafe = HiringCafeClient()
            data = await cafe.search(client)

    assert data == FIXTURE


import pytest

from src.hiringcafe import HiringCafeJob
from src.connectors.hiringcafe import HiringCafeConnector
from src.models import ConnectorState
from src.sightings import Sighting


def _job(family, slug, company="Acme", external_id="e1"):
    return HiringCafeJob(
        title="Software Engineer", company=company, ats_family=family, ats_slug=slug,
        external_id=external_id, apply_url=f"https://jobs.example/{slug}/1",
        posted_at=None, description="", location=None, remote=None,
    )


class _FakeCafe:
    def __init__(self, jobs):
        self._jobs = jobs

    async def fetch_jobs(self, client):
        return self._jobs


@pytest.mark.asyncio
async def test_connector_collects_sightings_for_foreign_postings():
    sightings: list[Sighting] = []
    conn = HiringCafeConnector(
        cafe=_FakeCafe([
            _job("greenhouse", "newco", company="NewCo"),
            _job("greenhouse", "known"),           # in active set → no sighting
            _job("workday", "acme", external_id=None),  # no external_id → still a sighting
        ]),
        active_set={("greenhouse", "known")},
        max_postings=100,
        sightings=sightings,
    )
    result = await conn.fetch(None, ConnectorState())
    assert len(result.postings) == 1  # external_id-less job is not a posting
    got = {(s.ats_family, s.slug) for s in sightings}
    assert got == {("greenhouse", "newco"), ("workday", "acme")}
    assert all(s.apply_url and s.company for s in sightings)


@pytest.mark.asyncio
async def test_connector_without_sightings_list_behaves_as_before():
    conn = HiringCafeConnector(
        cafe=_FakeCafe([_job("greenhouse", "newco")]),
        active_set=set(), max_postings=100,
    )
    result = await conn.fetch(None, ConnectorState())
    assert len(result.postings) == 1


def test_build_connectors_threads_sightings_to_hiringcafe():
    import yaml

    from src.config import AppConfig
    from src.connectors.base import build_connectors

    cfg = AppConfig.model_validate(yaml.safe_load("""
filters:
  titles: ["software engineer"]
  seniority_allow: ["mid", "senior"]
  location: {remote_must_be_us: true, allowed_cities: [], allow_unknown: true}
  comp_floor_usd: 0
  stack_any_of: ["python"]
quiet_hours: {timezone: "UTC", start: "23:00", end: "07:00"}
sources:
  greenhouse: []
  lever: []
  ashby: []
  workable: []
  hiringcafe: {enabled: true}
schedules: {ats_minutes: 10, slow_minutes: 15}
"""))
    sightings: list = []
    conns = build_connectors(cfg, "slow", sightings=sightings)
    cafe = [c for c in conns if c.name == "hiringcafe"]
    assert len(cafe) == 1
    assert cafe[0]._sightings is sightings


def test_build_connectors_active_set_includes_promoted_boards():
    """A promoted (ok) board must suppress hiring.cafe's duplicate emission:
    its (family, token) pair joins the connector's active_set."""
    import yaml

    from src.config import AppConfig
    from src.connectors.base import build_connectors
    from src.sqlite_db import connect
    from src.state_sqlite import SqliteDiscoveredBoardsStore

    cfg = AppConfig.model_validate(yaml.safe_load("""
filters:
  titles: ["software engineer"]
  seniority_allow: ["mid", "senior"]
  location: {remote_must_be_us: true, allowed_cities: [], allow_unknown: true}
  comp_floor_usd: 0
  stack_any_of: ["python"]
quiet_hours: {timezone: "UTC", start: "23:00", end: "07:00"}
sources:
  greenhouse: []
  lever: []
  ashby: []
  workable: []
  hiringcafe: {enabled: true}
schedules: {ats_minutes: 10, slow_minutes: 15}
"""))
    boards = SqliteDiscoveredBoardsStore(connect(":memory:"))
    boards.upsert_ok("acme.wd5.myworkdayjobs.com", name="Acme", family="workday",
                     identity={"tenant": "acme", "region": "wd5", "site": "Ext"},
                     connector_name="workday:acme:Ext")
    boards.upsert_ok("careers-steel.icims.com", name="Steel", family="jsonld",
                     identity={"family": "icims", "slug": "steel", "base_url": "https://careers-steel.icims.com"},
                     connector_name="jsonld:icims:steel")
    boards.upsert_candidate("x.wd1.myworkdayjobs.com", name="X", family="workday",
                            identity={"tenant": "x", "region": "wd1", "site": "S"},
                            connector_name="workday:x:S")
    conns = build_connectors(cfg, "slow", boards=boards)
    cafe = [c for c in conns if c.name == "hiringcafe"][0]
    assert ("workday", "acme") in cafe._active_set
    assert ("icims", "steel") in cafe._active_set
    assert ("workday", "x") not in cafe._active_set  # candidates still flow


# ---- fetch_jobs_multi (extra_queries support) ----
from src.hiringcafe import DEFAULT_QUERY, fetch_jobs_multi, parse_jobs as _parse_jobs

CAFE_FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "hiringcafe_example.json").read_text()
)


class _FakeCafeClient:
    """Records (query, location) pairs; returns the real fixture payload."""
    def __init__(self):
        self.calls: list[tuple[str, str | None]] = []

    async def search(self, client, *, query, location=None):
        self.calls.append((query, location))
        return CAFE_FIXTURE


@pytest.mark.asyncio
async def test_fetch_jobs_multi_runs_one_search_per_query():
    fake = _FakeCafeClient()
    await fetch_jobs_multi(fake, client=None, searches=[
        (DEFAULT_QUERY, None), ("software engineer europe", None),
    ])
    assert fake.calls == [(DEFAULT_QUERY, None), ("software engineer europe", None)]


@pytest.mark.asyncio
async def test_fetch_jobs_multi_dedups_across_queries():
    # Both searches return the identical fixture payload, so the second batch
    # must dedup to nothing: total == one batch's worth.
    fake = _FakeCafeClient()
    jobs = await fetch_jobs_multi(fake, client=None, searches=[("a", None), ("b", None)])
    assert len(jobs) == len(_parse_jobs(CAFE_FIXTURE))
    ids = [j.external_id for j in jobs if j.external_id]
    assert len(ids) == len(set(ids))


@pytest.mark.asyncio
async def test_fetch_jobs_multi_single_query_matches_parse_jobs():
    fake = _FakeCafeClient()
    jobs = await fetch_jobs_multi(fake, client=None, searches=[(DEFAULT_QUERY, None)])
    assert len(jobs) == len(_parse_jobs(CAFE_FIXTURE))


# ---- searchState fix (the SSR endpoint ignores a bare ?q= param) ----
@pytest.mark.asyncio
async def test_client_search_sends_searchstate_not_q():
    async with httpx.AsyncClient() as client:
        with respx.mock:
            route = respx.route(
                method="GET", url__regex=r"https://hiringcafe\.com/_next/data/.*"
            ).respond(200, json=FIXTURE)
            respx.route(method="GET", url="https://hiringcafe.com/jobs").respond(
                200,
                text='<html><script id="__NEXT_DATA__">{"buildId":"test-build-id-123"}</script></html>',
            )
            cafe = HiringCafeClient()
            await cafe.search(client, query="software engineer europe")

    params = dict(route.calls.last.request.url.params)
    assert "q" not in params
    state = json.loads(params["searchState"])
    assert state["searchQuery"] == "software engineer europe"
    assert state["dateFetchedPastNDays"] == 4
    assert state["sortBy"] == "date"
    assert "locations" not in state
    assert "defaultToUserLocation" not in state


# ---- ats_family alias normalization (live labels differ from our family names) ----
def _alias_hit(source, token="acme", id_="x1"):
    return {
        "source": source,
        "board_token": token,
        "id": id_,
        "apply_url": "https://jobs.example.com/x",
        "job_information": {"title": "Software Engineer"},
    }


def test_parse_jobs_normalizes_ats_family_aliases():
    payload = {"pageProps": {"ssrHits": [
        _alias_hit("grnhse"),
        _alias_hit("ICIMS2"),
        _alias_hit("taleo_careersection"),
        _alias_hit("lever"),
        _alias_hit("someunknownats"),
    ]}}
    fams = [j.ats_family for j in parse_jobs(payload)]
    assert fams == ["greenhouse", "icims", "taleo", "lever", "someunknownats"]


# ---- geo-scoped searches (searchState `locations` key) ----
from src.hiringcafe import _location_state, _search_state


def test_location_state_country_shape():
    """Verified-minimal country object: match is by short_name; long_name must
    be present but its value is ignored (live evidence in the 2026-07-11 spec)."""
    assert _location_state("DE") == {
        "types": ["country"],
        "formatted_address": "DE",
        "address_components": [
            {"long_name": "DE", "short_name": "DE", "types": ["country"]}
        ],
        "options": {},
    }


def test_location_state_continent_shape():
    """Continents match on formatted_address with empty address_components."""
    assert _location_state("Europe") == {
        "types": ["continent"],
        "formatted_address": "Europe",
        "address_components": [],
        "options": {},
    }


def test_search_state_without_location_is_byte_identical_to_before():
    """Regression pin: unpinned deployments must emit exactly the pre-change
    state — no locations key, no defaultToUserLocation key."""
    state = json.loads(_search_state("software engineer"))
    assert state == {
        "searchQuery": "software engineer",
        "dateFetchedPastNDays": 4,
        "sortBy": "date",
    }


def test_search_state_with_location_adds_locations_only():
    state = json.loads(_search_state("software engineer", "Europe"))
    assert state["locations"] == [_location_state("Europe")]
    assert set(state) == {"searchQuery", "dateFetchedPastNDays", "sortBy", "locations"}


@pytest.mark.asyncio
async def test_client_search_sends_locations_when_scoped():
    async with httpx.AsyncClient() as client:
        with respx.mock:
            route = respx.route(
                method="GET", url__regex=r"https://hiringcafe\.com/_next/data/.*"
            ).respond(200, json=FIXTURE)
            respx.route(method="GET", url="https://hiringcafe.com/jobs").respond(
                200,
                text='<html><script id="__NEXT_DATA__">{"buildId":"test-build-id-123"}</script></html>',
            )
            cafe = HiringCafeClient()
            await cafe.search(client, query="software engineer", location="US")

    state = json.loads(dict(route.calls.last.request.url.params)["searchState"])
    assert state["locations"] == [{
        "types": ["country"],
        "formatted_address": "US",
        "address_components": [
            {"long_name": "US", "short_name": "US", "types": ["country"]}
        ],
        "options": {},
    }]


@pytest.mark.asyncio
async def test_fetch_jobs_multi_threads_locations_through():
    fake = _FakeCafeClient()
    await fetch_jobs_multi(fake, client=None, searches=[
        (DEFAULT_QUERY, "US"), ("software engineer", "Europe"), ("plain", None),
    ])
    assert fake.calls == [
        (DEFAULT_QUERY, "US"), ("software engineer", "Europe"), ("plain", None),
    ]


def test_build_connectors_builds_search_pairs():
    """Primary query carries cfg.location; extra entries mix strings and
    {query, location} mappings, in order."""
    import yaml

    from src.config import AppConfig
    from src.connectors.base import build_connectors
    from src.hiringcafe import DEFAULT_QUERY as _DQ

    cfg = AppConfig.model_validate(yaml.safe_load("""
filters:
  titles: ["software engineer"]
  seniority_allow: ["mid", "senior"]
  location: {remote_must_be_us: true, allowed_cities: [], allow_unknown: true}
  comp_floor_usd: 0
  stack_any_of: ["python"]
quiet_hours: {timezone: "UTC", start: "23:00", end: "07:00"}
sources:
  greenhouse: []
  lever: []
  ashby: []
  workable: []
  hiringcafe:
    enabled: true
    location: US
    extra_queries:
      - staff platform engineer
      - {query: software engineer, location: Europe}
schedules: {ats_minutes: 10, slow_minutes: 15}
"""))
    conns = build_connectors(cfg, "slow")
    cafe = [c for c in conns if c.name == "hiringcafe"][0]
    assert cafe._cafe._searches == [
        (_DQ, "US"),
        ("staff platform engineer", None),
        ("software engineer", "Europe"),
    ]
