# tests/connectors/test_jsonld.py
import httpx
import pytest
import respx

from src.connectors.jsonld import JsonLdBoardConnector, _jobposting_from_jsonld
from src.models import ConnectorState, NormalizedPosting

_LD = '<script type="application/ld+json">%s</script>'


def test_extracts_plain_jobposting():
    html = _LD % '{"@type":"JobPosting","title":"Engineer","description":"<p>Build</p>"}'
    jp = _jobposting_from_jsonld(html)
    assert jp["title"] == "Engineer"


def test_extracts_from_list_wrapper():
    html = _LD % '[{"@type":"Organization","name":"Acme"},{"@type":"JobPosting","title":"SRE"}]'
    assert _jobposting_from_jsonld(html)["title"] == "SRE"


def test_extracts_from_graph_wrapper():
    html = _LD % '{"@graph":[{"@type":"WebPage"},{"@type":"JobPosting","title":"Data Eng"}]}'
    assert _jobposting_from_jsonld(html)["title"] == "Data Eng"


def test_ignores_non_jobposting_scripts():
    html = (_LD % '{"@type":"BreadcrumbList"}') + (_LD % '{"@type":"JobPosting","title":"QA"}')
    assert _jobposting_from_jsonld(html)["title"] == "QA"


def test_type_can_be_a_list():
    html = _LD % '{"@type":["JobPosting","Thing"],"title":"Hybrid"}'
    assert _jobposting_from_jsonld(html)["title"] == "Hybrid"


def test_malformed_json_returns_none():
    assert _jobposting_from_jsonld(_LD % "{not json") is None


def test_no_script_returns_none():
    assert _jobposting_from_jsonld("<html><body>nothing</body></html>") is None


_SF_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:g="http://base.google.com/ns/1.0"><channel>
<item>
  <title>Necropsy Technician I (Spencerville, OH, US, 45887)</title>
  <description><![CDATA[<p>Great role</p>]]></description>
  <link>https://jobs.aosmith.com/job/Spencerville-Necropsy/1401959500/</link>
  <guid isPermaLink="false">1401959500</guid>
  <g:id>1401959500</g:id>
  <g:location>Spencerville, OH, US, 45887</g:location>
</item>
</channel></rss>"""

_JOB_DETAIL = """<html><head>
<script type="application/ld+json">{"@type":"JobPosting","title":"Necropsy Technician I",
"description":"<p>Full JD here with responsibilities</p>","datePosted":"2026-07-01"}</script>
</head><body>...</body></html>"""


def _sf():
    return JsonLdBoardConnector(
        family="successfactors", slug="aosmith",
        base_url="https://jobs.aosmith.com", company="A. O. Smith",
    )


@respx.mock
@pytest.mark.asyncio
async def test_successfactors_fetch_parses_feed():
    respx.get("https://jobs.aosmith.com/jobsfeed.xml").mock(
        return_value=httpx.Response(200, text=_SF_FEED)
    )
    async with httpx.AsyncClient() as client:
        result = await _sf().fetch(client, ConnectorState())
    assert len(result.postings) == 1
    p = result.postings[0]
    assert p.source == "successfactors:aosmith"
    assert p.external_id == "1401959500"
    assert "Necropsy Technician" in p.title
    assert p.apply_url == "https://jobs.aosmith.com/job/Spencerville-Necropsy/1401959500/"
    assert p.location == "Spencerville, OH, US, 45887"
    assert p.company == "A. O. Smith"


@respx.mock
@pytest.mark.asyncio
async def test_successfactors_falls_back_to_sitemap_on_feed_404():
    respx.get("https://jobs.aosmith.com/jobsfeed.xml").mock(return_value=httpx.Response(404))
    respx.get("https://jobs.aosmith.com/sitemap.xml").mock(
        return_value=httpx.Response(200, text=_SF_FEED)
    )
    async with httpx.AsyncClient() as client:
        result = await _sf().fetch(client, ConnectorState())
    assert len(result.postings) == 1


@respx.mock
@pytest.mark.asyncio
async def test_successfactors_bare_sitemap_returns_empty():
    bare = '<?xml version="1.0"?><urlset><url><loc>https://x/job/1</loc></url></urlset>'
    respx.get("https://jobs.aosmith.com/jobsfeed.xml").mock(return_value=httpx.Response(404))
    respx.get("https://jobs.aosmith.com/sitemap.xml").mock(
        return_value=httpx.Response(200, text=bare)
    )
    async with httpx.AsyncClient() as client:
        result = await _sf().fetch(client, ConnectorState())
    assert result.postings == []


@respx.mock
@pytest.mark.asyncio
async def test_successfactors_double_404_raises_http_status_error():
    # Both /jobsfeed.xml and /sitemap.xml 404ing is a genuine "board is gone"
    # signal — it must surface as an HTTPStatusError (404) so poll_health's
    # classify_outcome sees "dead" (not "transient") and eventually suppresses
    # the board, instead of retrying it forever.
    respx.get("https://jobs.aosmith.com/jobsfeed.xml").mock(return_value=httpx.Response(404))
    respx.get("https://jobs.aosmith.com/sitemap.xml").mock(return_value=httpx.Response(404))
    async with httpx.AsyncClient() as client:
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await _sf().fetch(client, ConnectorState())
    assert exc_info.value.response.status_code == 404


@respx.mock
@pytest.mark.asyncio
async def test_enrich_swaps_in_jsonld_description():
    respx.get("https://jobs.aosmith.com/job/x/1/").mock(
        return_value=httpx.Response(200, text=_JOB_DETAIL)
    )
    posting = NormalizedPosting(
        job_id="successfactors:aosmith:1", title="Necropsy Technician I",
        company="A. O. Smith", location_text="Spencerville, OH", location_tags=frozenset(),
        seniority="mid", stack=frozenset(), comp_min=None, comp_max=None,
        apply_url="https://jobs.aosmith.com/job/x/1/", description="", posted_at=None,
        source="successfactors:aosmith",
    )
    async with httpx.AsyncClient() as client:
        out = await _sf().enrich(client, posting)
    assert "Full JD here" in out.description


@respx.mock
@pytest.mark.asyncio
async def test_enrich_missing_jsonld_returns_posting_unchanged():
    respx.get("https://jobs.aosmith.com/job/x/1/").mock(
        return_value=httpx.Response(200, text="<html>no ld+json</html>")
    )
    posting = NormalizedPosting(
        job_id="successfactors:aosmith:1", title="T", company="A. O. Smith",
        location_text="x", location_tags=frozenset(), seniority="mid", stack=frozenset(),
        comp_min=None, comp_max=None, apply_url="https://jobs.aosmith.com/job/x/1/",
        description="original", posted_at=None, source="successfactors:aosmith",
    )
    async with httpx.AsyncClient() as client:
        out = await _sf().enrich(client, posting)
    assert out.description == "original"


_ICIMS_PAGE = """<html><body><ul>
<li class="iCIMS_JobCardItem"><div class="row">
<div class="header left"><span>US-PA-Pittsburgh</span></div>
<div class="header right"><span>2026-7685</span></div>
<div class="title"><a href="https://careers-steeldynamics.icims.com/jobs/7685/customer-service-representative/job?in_iframe=1"><h3>Customer Service Representative</h3></a></div>
<div class="description">300 Miffin Road-Pittsburgh-PA-15207</div>
</div></li>
</ul></body></html>"""

_ICIMS_EMPTY = '<html><body><div class="iCIMS_NoResults">No Results</div></body></html>'


def _icims():
    return JsonLdBoardConnector(
        family="icims", slug="steeldynamics",
        base_url="https://careers-steeldynamics.icims.com", company="Steel Dynamics",
    )


@respx.mock
@pytest.mark.asyncio
async def test_icims_fetch_parses_cards_and_stops_on_empty_page():
    respx.get(url__regex=r".*/jobs/search.*pr=0.*").mock(
        return_value=httpx.Response(200, text=_ICIMS_PAGE)
    )
    respx.get(url__regex=r".*/jobs/search.*pr=1.*").mock(
        return_value=httpx.Response(200, text=_ICIMS_EMPTY)
    )
    async with httpx.AsyncClient() as client:
        result = await _icims().fetch(client, ConnectorState())
    assert len(result.postings) == 1
    p = result.postings[0]
    assert p.external_id == "7685"
    assert p.title == "Customer Service Representative"
    assert p.location == "US-PA-Pittsburgh"
    assert p.apply_url.startswith("https://careers-steeldynamics.icims.com/jobs/7685/")
    assert p.company == "Steel Dynamics"


_TB_PAGE_1 = """<html><body>
<section id="search-results" data-total-results="2" data-total-pages="2" data-current-page="1">
<ul>
<li><a href="/en/job/houston/senior-engineer/38138/79999"><h2>Senior Engineer</h2>
<span class="job-location">Houston, TX</span></a></li>
</ul></section></body></html>"""

_TB_PAGE_2 = """<html><body>
<section id="search-results" data-total-results="2" data-total-pages="2" data-current-page="2">
<ul>
<li><a href="/en/job/remote/data-analyst/38138/80000"><h2>Data Analyst</h2>
<span class="job-location">Remote</span></a></li>
</ul></section></body></html>"""


def _tb():
    return JsonLdBoardConnector(
        family="talentbrew", slug="chevron",
        base_url="https://jobs.chevron.com", company="Chevron",
    )


@respx.mock
@pytest.mark.asyncio
async def test_talentbrew_fetch_pages_through_all_results():
    respx.get(url__regex=r".*/search-jobs\?p=1.*").mock(return_value=httpx.Response(200, text=_TB_PAGE_1))
    respx.get(url__regex=r".*/search-jobs\?p=2.*").mock(return_value=httpx.Response(200, text=_TB_PAGE_2))
    async with httpx.AsyncClient() as client:
        result = await _tb().fetch(client, ConnectorState())
    ids = {p.external_id for p in result.postings}
    assert ids == {"79999", "80000"}
    p = next(p for p in result.postings if p.external_id == "79999")
    assert p.title == "Senior Engineer"
    assert p.location == "Houston, TX"
    assert p.apply_url == "https://jobs.chevron.com/en/job/houston/senior-engineer/38138/79999"
    assert p.company == "Chevron"
