# tests/connectors/test_eightfold.py
import httpx
import pytest
import respx

from src.connectors.eightfold import EightfoldConnector, _parse_epoch
from src.models import ConnectorState, NormalizedPosting

_PCSX_P0 = {"status": 200, "data": {"count": 3, "positions": [
    {"id": 137480700296, "name": "Senior Data Scientist", "locations": ["Hyderabad - TS - IN"],
     "postedTs": 1782950400, "positionUrl": "/careers/job/137480700296"},
    {"id": 137480700297, "name": "Staff SRE", "locations": ["Princeton, NJ"],
     "postedTs": 1782864000, "positionUrl": "/careers/job/137480700297"},
]}}
_PCSX_P1 = {"status": 200, "data": {"count": 3, "positions": [
    {"id": 137480700297, "name": "Staff SRE", "locations": ["Princeton, NJ"],
     "postedTs": 1782864000, "positionUrl": "/careers/job/137480700297"},  # overlap w/ p0
    {"id": 137480700298, "name": "ML Engineer", "locations": ["Remote - US"],
     "postedTs": 1782777600, "positionUrl": "/careers/job/137480700298"},
]}}

_JOB_DETAIL = """<html><head>
<script type="application/ld+json">{"@type":"JobPosting","title":"Senior Data Scientist",
"description":"<p>Own the data science roadmap.</p>","datePosted":"2026-07-01"}</script>
</head><body>x</body></html>"""


def _bms():
    return EightfoldConnector(slug="bms", domain="bms.com", flavor="pcsx",
                              company="Bristol Myers Squibb")


def test_parse_epoch_seconds_and_millis():
    from datetime import datetime, timezone
    assert _parse_epoch(1782864000) == datetime(2026, 7, 1, tzinfo=timezone.utc)
    assert _parse_epoch(1782864000000) == datetime(2026, 7, 1, tzinfo=timezone.utc)
    assert _parse_epoch(None) is None
    assert _parse_epoch("nope") is None


@respx.mock
@pytest.mark.asyncio
async def test_pcsx_fetch_paginates_and_dedupes_by_id():
    respx.get(url__regex=r".*/api/pcsx/search.*start=0.*").mock(
        return_value=httpx.Response(200, json=_PCSX_P0))
    respx.get(url__regex=r".*/api/pcsx/search.*start=10.*").mock(
        return_value=httpx.Response(200, json=_PCSX_P1))
    async with httpx.AsyncClient() as client:
        result = await _bms().fetch(client, ConnectorState())
    ids = [p.external_id for p in result.postings]
    assert ids == ["137480700296", "137480700297", "137480700298"]  # 297 not double-emitted
    p = result.postings[0]
    assert p.source == "eightfold:bms"
    assert p.title == "Senior Data Scientist"
    assert p.location == "Hyderabad - TS - IN"
    assert p.apply_url == "https://bms.eightfold.ai/careers/job/137480700296"
    assert p.company == "Bristol Myers Squibb"


@respx.mock
@pytest.mark.asyncio
async def test_pcsx_first_page_failure_raises():
    respx.get(url__regex=r".*/api/pcsx/search.*").mock(return_value=httpx.Response(500))
    with pytest.raises(httpx.HTTPStatusError):
        async with httpx.AsyncClient() as client:
            await _bms().fetch(client, ConnectorState())


@respx.mock
@pytest.mark.asyncio
async def test_enrich_swaps_in_jsonld_description():
    respx.get(url__regex=r".*/careers/job/137480700296.*").mock(
        return_value=httpx.Response(200, text=_JOB_DETAIL))
    posting = NormalizedPosting(
        job_id="eightfold:bms:137480700296", title="Senior Data Scientist",
        company="Bristol Myers Squibb", location_text="Hyderabad", location_tags=frozenset(),
        seniority="senior", stack=frozenset(), comp_min=None, comp_max=None,
        apply_url="https://bms.eightfold.ai/careers/job/137480700296", description="",
        posted_at=None, source="eightfold:bms")
    async with httpx.AsyncClient() as client:
        out = await _bms().enrich(client, posting)
    assert "Own the data science roadmap" in out.description


_APPLYV2 = {"count": 1, "positions": [
    {"id": 1099554463102, "name": "Procurement Manager", "location": "Charlotte, NC",
     "t_create": 1782864000000,  # millis
     "canonicalPositionUrl": "https://albemarle.eightfold.ai/careers/job/1099554463102"},
]}
_PCSX_403 = {"status": 403, "message": "PCSX is not enabled for this user."}


@respx.mock
@pytest.mark.asyncio
async def test_apply_v2_fetch_parses_top_level_positions():
    respx.get(url__regex=r".*/api/apply/v2/jobs.*start=0.*").mock(
        return_value=httpx.Response(200, json=_APPLYV2))
    conn = EightfoldConnector(slug="albemarle", domain="albemarle.com", flavor="apply_v2",
                              company="Albemarle")
    async with httpx.AsyncClient() as client:
        result = await conn.fetch(client, ConnectorState())
    assert len(result.postings) == 1
    p = result.postings[0]
    assert p.external_id == "1099554463102"
    assert p.apply_url == "https://albemarle.eightfold.ai/careers/job/1099554463102"  # absolute
    from datetime import datetime, timezone
    assert p.posted_at == datetime(2026, 7, 1, tzinfo=timezone.utc)  # millis parsed


@respx.mock
@pytest.mark.asyncio
async def test_flavor_fallback_pcsx_403_to_apply_v2():
    # stored flavor pcsx returns a 403 flavor-error → connector retries apply_v2
    respx.get(url__regex=r".*/api/pcsx/search.*").mock(
        return_value=httpx.Response(403, json=_PCSX_403))
    respx.get(url__regex=r".*/api/apply/v2/jobs.*start=0.*").mock(
        return_value=httpx.Response(200, json=_APPLYV2))
    conn = EightfoldConnector(slug="albemarle", domain="albemarle.com", flavor="pcsx",
                              company="Albemarle")
    async with httpx.AsyncClient() as client:
        result = await conn.fetch(client, ConnectorState())
    assert len(result.postings) == 1
    assert result.postings[0].external_id == "1099554463102"


@respx.mock
@pytest.mark.asyncio
async def test_non_flavor_403_propagates():
    # a 403 WITHOUT the flavor-error markers is a real failure, not a fallback trigger
    respx.get(url__regex=r".*/api/pcsx/search.*").mock(
        return_value=httpx.Response(403, json={"message": "Forbidden"}))
    conn = EightfoldConnector(slug="x", domain="x.com", flavor="pcsx")
    with pytest.raises(httpx.HTTPStatusError):
        async with httpx.AsyncClient() as client:
            await conn.fetch(client, ConnectorState())
