# tests/connectors/test_taleo.py
from datetime import datetime, timezone

import httpx
import pytest
import respx

from src.connectors.taleo import _classify_columns, _search_body, TaleoConnector, _extract_description
from src.models import ConnectorState, NormalizedPosting


def test_search_body_has_tz_facets_and_pageno():
    b = _search_body(3)
    assert b["pageNo"] == 3
    assert b["sortingSelection"]["sortBySelectionParam"] == "3"
    assert b["multilineEnabled"] is False
    ids = {f["id"] for f in b["filterSelectionParam"]["searchFilterSelections"]}
    assert ids == {"POSTING_DATE", "LOCATION", "JOB_FIELD", "JOB_TYPE", "JOB_SCHEDULE", "JOB_LEVEL"}
    adv = {f["id"] for f in b["advancedSearchFiltersSelectionParam"]["searchFilterSelections"]}
    assert adv == {"ORGANIZATION", "LOCATION", "JOB_FIELD", "JOB_NUMBER",
                   "URGENT_JOB", "EMPLOYEE_STATUS", "WILL_TRAVEL", "JOB_SHIFT"}


def test_classify_columns_textron_shape_title_first():
    # textron: [title, location(JSON), date] — contestNo NOT in column
    title, loc, posted = _classify_columns(
        ["Engineer I", '["India-Bangalore"]', "07/03/2026"], contest_no="341857")
    assert title == "Engineer I"
    assert loc == "India-Bangalore"
    assert posted == datetime(2026, 7, 3, tzinfo=timezone.utc)


def test_classify_columns_cinfin_shape_contestno_first_no_date():
    # cinfin: [contestNo, title, location(JSON)] — no date column
    title, loc, posted = _classify_columns(
        ["2600499", "Senior Corporate Recruiter - Talent Acquisition", '["OH-Cincinnati"]'],
        contest_no="2600499")
    assert title == "Senior Corporate Recruiter - Talent Acquisition"
    assert loc == "OH-Cincinnati"
    assert posted is None


def test_classify_columns_multi_location_joins():
    title, loc, _ = _classify_columns(
        ["Staff Eng", '["US-TX-Austin","US-CA-SF"]'], contest_no="9")
    assert title == "Staff Eng"
    assert loc == "US-TX-Austin; US-CA-SF"


def test_classify_columns_no_title_returns_none():
    title, loc, posted = _classify_columns(["7", "07/01/2026"], contest_no="7")
    assert title is None
    assert posted == datetime(2026, 7, 1, tzinfo=timezone.utc)


_JOBSEARCH_HTML = "<html><script>var cfg = { portalNo: '8140753014', x:1 };</script></html>"


def _page(reqs, total, page):
    return {"requisitionList": reqs,
            "pagingData": {"currentPageNo": page, "pageSize": 25, "totalCount": total}}


_REQ_TEXTRON = {"jobId": "1538266", "contestNo": "341857",
                "column": ["Engineer I", '["India-Bangalore"]', "07/03/2026"]}
_REQ_CINFIN = {"jobId": "1", "contestNo": "2600499",
               "column": ["2600499", "Senior Corporate Recruiter", '["OH-Cincinnati"]']}


def _conn():
    return TaleoConnector(tenant="textron", section="textron", company="Textron")


@respx.mock
@pytest.mark.asyncio
async def test_fetch_gets_portalno_then_posts_searchjobs():
    respx.get("https://textron.taleo.net/careersection/textron/jobsearch.ftl?lang=en").mock(
        return_value=httpx.Response(200, text=_JOBSEARCH_HTML))
    route = respx.post(url__regex=r".*/rest/jobboard/searchjobs.*portal=8140753014.*").mock(
        return_value=httpx.Response(200, json=_page([_REQ_TEXTRON], total=1, page=1)))
    async with httpx.AsyncClient() as client:
        result = await _conn().fetch(client, ConnectorState())
    assert len(result.postings) == 1
    p = result.postings[0]
    assert p.source == "taleo:textron:textron"
    assert p.external_id == "341857"
    assert p.title == "Engineer I"
    assert p.location == "India-Bangalore"
    assert p.apply_url == "https://textron.taleo.net/careersection/textron/jobdetail.ftl?job=341857"
    assert p.company == "Textron"
    # verify the POST body carried the facet selections + tz header
    sent = route.calls.last.request
    assert sent.headers["tz"] == "GMT-05:00"
    import json as _j
    body = _j.loads(sent.content)
    assert body["pageNo"] == 1
    assert len(body["filterSelectionParam"]["searchFilterSelections"]) == 6


@respx.mock
@pytest.mark.asyncio
async def test_fetch_paginates_until_totalcount():
    respx.get(url__regex=r".*/jobsearch\.ftl.*").mock(
        return_value=httpx.Response(200, text=_JOBSEARCH_HTML))
    # totalCount 40, pageSize 25 → 2 pages
    respx.post(url__regex=r".*/searchjobs.*").mock(side_effect=[
        httpx.Response(200, json=_page([_REQ_TEXTRON], total=40, page=1)),
        httpx.Response(200, json=_page([dict(_REQ_CINFIN)], total=40, page=2)),
    ])
    async with httpx.AsyncClient() as client:
        result = await _conn().fetch(client, ConnectorState())
    assert {p.external_id for p in result.postings} == {"341857", "2600499"}


@respx.mock
@pytest.mark.asyncio
async def test_fetch_raises_when_no_portalno():
    respx.get(url__regex=r".*/jobsearch\.ftl.*").mock(
        return_value=httpx.Response(200, text="<html>no portal here</html>"))
    with pytest.raises(Exception):
        async with httpx.AsyncClient() as client:
            await _conn().fetch(client, ConnectorState())


@respx.mock
@pytest.mark.asyncio
async def test_fetch_cinfin_column_order_titles_correctly():
    respx.get(url__regex=r".*/jobsearch\.ftl.*").mock(
        return_value=httpx.Response(200, text="<html><script>portalNo: '101430233'</script></html>"))
    respx.post(url__regex=r".*/searchjobs.*").mock(
        return_value=httpx.Response(200, json=_page([_REQ_CINFIN], total=1, page=1)))
    conn = TaleoConnector(tenant="cinfin", section="ex", company="Cincinnati Financial")
    async with httpx.AsyncClient() as client:
        result = await conn.fetch(client, ConnectorState())
    p = result.postings[0]
    assert p.title == "Senior Corporate Recruiter"   # column[1], not column[0]
    assert p.location == "OH-Cincinnati"


# Real jobdetail shape: description is a percent-encoded, !*!-marked element in
# a fillList array of single-quoted JS strings (commas inside the values).
_DETAIL_HTML = (
    "<html><body><script>\n"
    "api.fillList('requisitionDescriptionInterface', 'descRequisition', ["
    "'Some Title, Inc.',"
    "'!*!%3Cp%3EPlan and control%2C end to end%2C all technical issues.%3C%2Fp%3E',"
    "'!*!%3Cp%3EQualifications here%3C%2Fp%3E'"
    "]);\n</script></body></html>"
)


def test_extract_description_decodes_first_marked_element():
    desc = _extract_description(_DETAIL_HTML)
    assert "Plan and control, end to end, all technical issues." in desc
    assert "Qualifications" not in desc  # only the FIRST !*! element (the Description)


def test_extract_description_missing_filllist_returns_none():
    assert _extract_description("<html>no fillList</html>") is None


# Regression: a literal `]` (and `[`) INSIDE the single-quoted JS string, ahead
# of the real array-closing `]`. A naive `.find(']')` scanner would truncate
# right after "Duties]" and miss everything past it; the string-aware
# bracket-balanced scanner must not.
_DETAIL_HTML_BRACKET_IN_STRING = (
    "<html><body><script>\n"
    "api.fillList('requisitionDescriptionInterface', 'descRequisition', "
    "['!*!Duties] include [several] things']);\n"
    "</script></body></html>"
)


def test_extract_description_bracket_inside_string_not_truncated():
    desc = _extract_description(_DETAIL_HTML_BRACKET_IN_STRING)
    assert "things" in desc


@respx.mock
@pytest.mark.asyncio
async def test_enrich_swaps_in_decoded_description():
    respx.get("https://textron.taleo.net/careersection/textron/jobdetail.ftl?job=341857").mock(
        return_value=httpx.Response(200, text=_DETAIL_HTML))
    posting = NormalizedPosting(
        job_id="taleo:textron:textron:341857", title="Engineer I", company="Textron",
        location_text="India-Bangalore", location_tags=frozenset(), seniority="mid",
        stack=frozenset(), comp_min=None, comp_max=None,
        apply_url="https://textron.taleo.net/careersection/textron/jobdetail.ftl?job=341857",
        description="", posted_at=None, source="taleo:textron:textron")
    async with httpx.AsyncClient() as client:
        out = await _conn().enrich(client, posting)
    assert "Plan and control" in out.description


@respx.mock
@pytest.mark.asyncio
async def test_enrich_missing_filllist_returns_unchanged():
    respx.get(url__regex=r".*/jobdetail\.ftl.*").mock(
        return_value=httpx.Response(200, text="<html>no fillList</html>"))
    posting = NormalizedPosting(
        job_id="taleo:textron:textron:1", title="T", company="Textron",
        location_text="x", location_tags=frozenset(), seniority="mid", stack=frozenset(),
        comp_min=None, comp_max=None,
        apply_url="https://textron.taleo.net/careersection/textron/jobdetail.ftl?job=1",
        description="original", posted_at=None, source="taleo:textron:textron")
    async with httpx.AsyncClient() as client:
        out = await _conn().enrich(client, posting)
    assert out.description == "original"


@respx.mock
@pytest.mark.asyncio
async def test_enrich_network_error_returns_unchanged():
    respx.get(url__regex=r".*/jobdetail\.ftl.*").mock(side_effect=httpx.ConnectError("boom"))
    posting = NormalizedPosting(
        job_id="taleo:textron:textron:1", title="T", company="Textron",
        location_text="x", location_tags=frozenset(), seniority="mid", stack=frozenset(),
        comp_min=None, comp_max=None,
        apply_url="https://textron.taleo.net/careersection/textron/jobdetail.ftl?job=1",
        description="original", posted_at=None, source="taleo:textron:textron")
    async with httpx.AsyncClient() as client:
        out = await _conn().enrich(client, posting)
    assert out.description == "original"
