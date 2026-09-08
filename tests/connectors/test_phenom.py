# tests/connectors/test_phenom.py
from datetime import datetime, timezone

import httpx
import pytest
import respx

from src.connectors.phenom import (
    PhenomConnector, _slug, _sitemap_roots_from_robots, _parse_sitemap,
    _parse_lastmod, _parse_posted, _jsonld_location, _jsonld_identifier,
)
from src.models import ConnectorState

_ROBOTS = """User-agent: *
Disallow: /apply/
Sitemap: https://careers.fisglobal.com/us/en/sitemap.xml
Sitemap: https://careers.fisglobal.com/global/en/sitemap.xml
"""

_INDEX = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://careers.fisglobal.com/us/en/sitemap_jobs_1.xml</loc></sitemap>
  <sitemap><loc>https://careers.fisglobal.com/us/en/sitemap_jobs_2.xml</loc></sitemap>
</sitemapindex>"""

_URLSET = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://careers.fisglobal.com/us/en/job/JR0294450/Sales-Exec</loc>
       <lastmod>2026-07-02T08:00:00+00:00</lastmod></url>
  <url><loc>https://careers.fisglobal.com/us/en/job/JR0294451/SRE</loc>
       <lastmod>2026-07-01</lastmod></url>
  <url><loc>https://careers.fisglobal.com/us/en/about</loc>
       <lastmod>2026-06-01</lastmod></url>
</urlset>"""


def test_slug_strips_prefix_and_hyphenates():
    assert _slug("https://careers.fisglobal.com") == "fisglobal-com"
    assert _slug("https://jobs.gehealthcare.com") == "gehealthcare-com"
    assert _slug("https://www.phenom-example.com/") == "phenom-example-com"


def test_sitemap_roots_from_robots():
    roots = _sitemap_roots_from_robots(_ROBOTS, "https://careers.fisglobal.com")
    assert roots == [
        "https://careers.fisglobal.com/us/en/sitemap.xml",
        "https://careers.fisglobal.com/global/en/sitemap.xml",
    ]


def test_sitemap_roots_falls_back_when_robots_has_none():
    roots = _sitemap_roots_from_robots("User-agent: *\nDisallow:\n",
                                       "https://careers.fisglobal.com")
    assert roots == ["https://careers.fisglobal.com/sitemap.xml"]


def test_parse_sitemap_index():
    kind, items = _parse_sitemap(_INDEX)
    assert kind == "index"
    assert items == [
        "https://careers.fisglobal.com/us/en/sitemap_jobs_1.xml",
        "https://careers.fisglobal.com/us/en/sitemap_jobs_2.xml",
    ]


def test_parse_sitemap_urlset_filters_to_jobs():
    kind, items = _parse_sitemap(_URLSET)
    assert kind == "urlset"
    locs = [loc for loc, _ in items]
    assert "https://careers.fisglobal.com/us/en/job/JR0294450/Sales-Exec" in locs
    assert all("/job/" in loc for loc in locs)  # /about filtered out
    assert len(items) == 2


def test_parse_lastmod_date_and_datetime():
    assert _parse_lastmod("2026-07-01") == datetime(2026, 7, 1, tzinfo=timezone.utc)
    assert _parse_lastmod("2026-07-02T08:00:00+00:00") == datetime(2026, 7, 2, 8, 0, tzinfo=timezone.utc)
    assert _parse_lastmod(None) is None
    assert _parse_lastmod("garbage") is None


def test_parse_posted_naive_gets_utc():
    assert _parse_posted("2026-07-01") == datetime(2026, 7, 1, tzinfo=timezone.utc)
    assert _parse_posted(None) is None


def test_jsonld_identifier_string_dict_and_fallback():
    assert _jsonld_identifier({"identifier": "JR0294450"}, "https://x/job/URLID/slug") == "JR0294450"
    assert _jsonld_identifier(
        {"identifier": {"@type": "PropertyValue", "value": "JR999"}}, "https://x/job/URLID/slug"
    ) == "JR999"
    # no identifier → last non-empty path segment before the human slug: use the /job/{ID}/ segment
    assert _jsonld_identifier({}, "https://x/us/en/job/URLID/human-slug") == "URLID"


def test_jsonld_location_joins_address():
    jp = {"jobLocation": {"address": {"addressLocality": "Jacksonville",
                                      "addressRegion": "FL", "addressCountry": "US"}}}
    assert _jsonld_location(jp) == "Jacksonville, FL, US"
    assert _jsonld_location({}) is None


_ROBOTS_ONE = """User-agent: *
Sitemap: https://careers.fisglobal.com/sitemap.xml
"""

_URLSET_2JOBS = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://careers.fisglobal.com/job/JR1/New-Role</loc>
       <lastmod>2026-07-02T08:00:00+00:00</lastmod></url>
  <url><loc>https://careers.fisglobal.com/job/JR2/Old-Role</loc>
       <lastmod>2026-06-01T08:00:00+00:00</lastmod></url>
</urlset>"""

def _job_page(title, ident, desc="<p>Do the thing.</p>", date="2026-07-02", loc_city="Jacksonville"):
    return f"""<html><head>
<script type="application/ld+json">{{"@type":"JobPosting","title":"{title}",
"identifier":"{ident}","description":"{desc}","datePosted":"{date}",
"jobLocation":{{"address":{{"addressLocality":"{loc_city}","addressRegion":"FL","addressCountry":"US"}}}}}}</script>
</head><body>x</body></html>"""


def _fis():
    return PhenomConnector(careers_url="https://careers.fisglobal.com", company="FIS")


@respx.mock
@pytest.mark.asyncio
async def test_fetch_first_run_gets_recent_job_only():
    # First run (no watermark): first_run_days=3 from "now". JR1 (2026-07-02) is
    # recent; JR2 (2026-06-01) is older than 3 days → not fetched.
    # respx has no clock; the connector uses real now(), so this test freezes time.
    from freezegun import freeze_time
    respx.get("https://careers.fisglobal.com/robots.txt").mock(
        return_value=httpx.Response(200, text=_ROBOTS_ONE))
    respx.get("https://careers.fisglobal.com/sitemap.xml").mock(
        return_value=httpx.Response(200, text=_URLSET_2JOBS))
    respx.get("https://careers.fisglobal.com/job/JR1/New-Role").mock(
        return_value=httpx.Response(200, text=_job_page("New Role", "JR1")))
    with freeze_time("2026-07-03"):
        async with httpx.AsyncClient() as client:
            result = await _fis().fetch(client, ConnectorState())
    assert len(result.postings) == 1
    p = result.postings[0]
    assert p.source == "phenom:fisglobal-com"
    assert p.external_id == "JR1"
    assert p.title == "New Role"
    assert "Do the thing" in p.description
    assert p.location == "Jacksonville, FL, US"
    assert p.apply_url == "https://careers.fisglobal.com/job/JR1/New-Role"
    assert p.company == "FIS"
    # watermark advances to the max lastmod across ALL job entries (incl. JR2)
    assert result.new_state.last_modified == "2026-07-02T08:00:00+00:00"


@respx.mock
@pytest.mark.asyncio
async def test_fetch_watermark_diff_skips_old_jobs():
    # Watermark at 2026-07-01: JR1 (07-02) is newer → fetched; JR2 (06-01) is
    # older than watermark-overlap → skipped. No freeze needed (watermark path).
    respx.get("https://careers.fisglobal.com/robots.txt").mock(
        return_value=httpx.Response(200, text=_ROBOTS_ONE))
    respx.get("https://careers.fisglobal.com/sitemap.xml").mock(
        return_value=httpx.Response(200, text=_URLSET_2JOBS))
    jr1 = respx.get("https://careers.fisglobal.com/job/JR1/New-Role").mock(
        return_value=httpx.Response(200, text=_job_page("New Role", "JR1")))
    async with httpx.AsyncClient() as client:
        result = await _fis().fetch(
            client, ConnectorState(last_modified="2026-07-01T00:00:00+00:00"))
    assert [p.external_id for p in result.postings] == ["JR1"]
    assert jr1.called


@respx.mock
@pytest.mark.asyncio
async def test_fetch_walks_sitemapindex():
    index = """<?xml version="1.0"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://careers.fisglobal.com/sm1.xml</loc></sitemap>
</sitemapindex>"""
    respx.get("https://careers.fisglobal.com/robots.txt").mock(
        return_value=httpx.Response(200, text=_ROBOTS_ONE))
    respx.get("https://careers.fisglobal.com/sitemap.xml").mock(
        return_value=httpx.Response(200, text=index))
    respx.get("https://careers.fisglobal.com/sm1.xml").mock(
        return_value=httpx.Response(200, text=_URLSET_2JOBS))
    respx.get("https://careers.fisglobal.com/job/JR1/New-Role").mock(
        return_value=httpx.Response(200, text=_job_page("New Role", "JR1")))
    async with httpx.AsyncClient() as client:
        result = await _fis().fetch(
            client, ConnectorState(last_modified="2026-07-01T00:00:00+00:00"))
    assert [p.external_id for p in result.postings] == ["JR1"]


@respx.mock
@pytest.mark.asyncio
async def test_fetch_skips_job_page_missing_jsonld():
    respx.get("https://careers.fisglobal.com/robots.txt").mock(
        return_value=httpx.Response(200, text=_ROBOTS_ONE))
    respx.get("https://careers.fisglobal.com/sitemap.xml").mock(
        return_value=httpx.Response(200, text=_URLSET_2JOBS))
    respx.get("https://careers.fisglobal.com/job/JR1/New-Role").mock(
        return_value=httpx.Response(200, text="<html>no ld+json</html>"))
    async with httpx.AsyncClient() as client:
        result = await _fis().fetch(
            client, ConnectorState(last_modified="2026-07-01T00:00:00+00:00"))
    assert result.postings == []  # JR1 skipped (no JSON-LD), JR2 below watermark


@respx.mock
@pytest.mark.asyncio
async def test_fetch_raises_when_no_sitemap():
    respx.get("https://careers.fisglobal.com/robots.txt").mock(
        return_value=httpx.Response(404))
    respx.get("https://careers.fisglobal.com/sitemap.xml").mock(
        return_value=httpx.Response(500))
    with pytest.raises(httpx.HTTPStatusError):
        async with httpx.AsyncClient() as client:
            await _fis().fetch(client, ConnectorState())


@respx.mock
@pytest.mark.asyncio
async def test_fetch_jsonld_location_list_joins_address():
    # jobLocation as a list (single entry) exercises _jsonld_location's list branch.
    job_page = """<html><head>
<script type="application/ld+json">{"@type":"JobPosting","title":"List Loc Role",
"identifier":"JR1","description":"<p>Do the thing.</p>","datePosted":"2026-07-02",
"jobLocation":[{"address":{"addressLocality":"Jacksonville","addressRegion":"FL","addressCountry":"US"}}]}</script>
</head><body>x</body></html>"""
    respx.get("https://careers.fisglobal.com/robots.txt").mock(
        return_value=httpx.Response(200, text=_ROBOTS_ONE))
    respx.get("https://careers.fisglobal.com/sitemap.xml").mock(
        return_value=httpx.Response(200, text=_URLSET_2JOBS))
    respx.get("https://careers.fisglobal.com/job/JR1/New-Role").mock(
        return_value=httpx.Response(200, text=job_page))
    async with httpx.AsyncClient() as client:
        result = await _fis().fetch(
            client, ConnectorState(last_modified="2026-07-01T00:00:00+00:00"))
    assert len(result.postings) == 1
    assert result.postings[0].location == "Jacksonville, FL, US"


@respx.mock
@pytest.mark.asyncio
async def test_fetch_identifier_falls_back_to_url_segment_when_missing():
    # No "identifier" field at all → external_id derives from the /job/{ID}/ path segment.
    job_page = """<html><head>
<script type="application/ld+json">{"@type":"JobPosting","title":"No Ident Role",
"description":"<p>Do the thing.</p>","datePosted":"2026-07-02",
"jobLocation":{"address":{"addressLocality":"Jacksonville","addressRegion":"FL","addressCountry":"US"}}}</script>
</head><body>x</body></html>"""
    respx.get("https://careers.fisglobal.com/robots.txt").mock(
        return_value=httpx.Response(200, text=_ROBOTS_ONE))
    respx.get("https://careers.fisglobal.com/sitemap.xml").mock(
        return_value=httpx.Response(200, text=_URLSET_2JOBS))
    respx.get("https://careers.fisglobal.com/job/JR1/New-Role").mock(
        return_value=httpx.Response(200, text=job_page))
    async with httpx.AsyncClient() as client:
        result = await _fis().fetch(
            client, ConnectorState(last_modified="2026-07-01T00:00:00+00:00"))
    assert len(result.postings) == 1
    assert result.postings[0].external_id == "JR1"


@respx.mock
@pytest.mark.asyncio
async def test_fetch_malformed_sitemap_xml_yields_no_postings_without_raising():
    # got_any becomes True (200 response received) so no raise; _parse_sitemap
    # raises ET.ParseError which _collect_job_entries catches → 0 entries.
    respx.get("https://careers.fisglobal.com/robots.txt").mock(
        return_value=httpx.Response(200, text=_ROBOTS_ONE))
    respx.get("https://careers.fisglobal.com/sitemap.xml").mock(
        return_value=httpx.Response(200, text="<not-xml"))
    async with httpx.AsyncClient() as client:
        result = await _fis().fetch(client, ConnectorState())
    assert result.postings == []
    assert result.new_state is None


@respx.mock
@pytest.mark.asyncio
async def test_fetch_watermark_never_regresses():
    # Prior watermark is newer than every entry's lastmod (e.g. the newest job
    # was delisted, or a sub-sitemap transiently failed on a prior run), so
    # max(all_lm) alone would be OLDER than the prior watermark. The
    # watermark must hold, not regress — this is the discriminating case for
    # the old `max(all_lm) if all_lm else watermark` code.
    urlset = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://careers.fisglobal.com/job/JR1/New-Role</loc>
       <lastmod>2026-07-01T00:00:00+00:00</lastmod></url>
</urlset>"""
    respx.get("https://careers.fisglobal.com/robots.txt").mock(
        return_value=httpx.Response(200, text=_ROBOTS_ONE))
    respx.get("https://careers.fisglobal.com/sitemap.xml").mock(
        return_value=httpx.Response(200, text=urlset))
    async with httpx.AsyncClient() as client:
        result = await _fis().fetch(
            client, ConnectorState(last_modified="2026-07-03T00:00:00+00:00"))
    assert result.postings == []  # JR1 (07-01) is below the watermark-overlap window
    assert result.new_state.last_modified == "2026-07-03T00:00:00+00:00"


@respx.mock
@pytest.mark.asyncio
async def test_fetch_falls_back_to_sitemap_xml_when_advertised_sitemap_404s():
    # robots.txt advertises a sitemap URL that 404s (a stale directive on an
    # otherwise-live board). The plain /sitemap.xml fallback must still be
    # tried before the connector gives up and raises.
    robots = "User-agent: *\nSitemap: https://careers.fisglobal.com/us/en/sitemap.xml\n"
    respx.get("https://careers.fisglobal.com/robots.txt").mock(
        return_value=httpx.Response(200, text=robots))
    respx.get("https://careers.fisglobal.com/us/en/sitemap.xml").mock(
        return_value=httpx.Response(404))
    respx.get("https://careers.fisglobal.com/sitemap.xml").mock(
        return_value=httpx.Response(200, text=_URLSET_2JOBS))
    respx.get("https://careers.fisglobal.com/job/JR1/New-Role").mock(
        return_value=httpx.Response(200, text=_job_page("New Role", "JR1")))
    async with httpx.AsyncClient() as client:
        result = await _fis().fetch(
            client, ConnectorState(last_modified="2026-07-01T00:00:00+00:00"))
    assert [p.external_id for p in result.postings] == ["JR1"]


@respx.mock
@pytest.mark.asyncio
async def test_fetch_overlap_reincludes_job_just_before_watermark():
    # since = watermark - 30min overlap. A job lastmod 10 min before the raw
    # watermark falls inside that overlap window and must still be fetched
    # (guards a job that was mid-update right at watermark time).
    urlset = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://careers.fisglobal.com/job/JR1/New-Role</loc>
       <lastmod>2026-07-02T07:50:00+00:00</lastmod></url>
</urlset>"""
    respx.get("https://careers.fisglobal.com/robots.txt").mock(
        return_value=httpx.Response(200, text=_ROBOTS_ONE))
    respx.get("https://careers.fisglobal.com/sitemap.xml").mock(
        return_value=httpx.Response(200, text=urlset))
    respx.get("https://careers.fisglobal.com/job/JR1/New-Role").mock(
        return_value=httpx.Response(200, text=_job_page("New Role", "JR1")))
    async with httpx.AsyncClient() as client:
        result = await _fis().fetch(
            client, ConnectorState(last_modified="2026-07-02T08:00:00+00:00"))
    assert [p.external_id for p in result.postings] == ["JR1"]


@respx.mock
@pytest.mark.asyncio
async def test_fetch_dedupes_same_loc_across_multiple_sitemaps():
    # A multi-locale board (e.g. /us/en/ + /global/en/) can list the same
    # canonical job loc in more than one robots-advertised sitemap — it must
    # be detail-fetched (and counted) only once.
    urlset = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://careers.fisglobal.com/us/en/job/JR1/New-Role</loc>
       <lastmod>2026-07-02T08:00:00+00:00</lastmod></url>
</urlset>"""
    respx.get("https://careers.fisglobal.com/robots.txt").mock(
        return_value=httpx.Response(200, text=_ROBOTS))
    respx.get("https://careers.fisglobal.com/us/en/sitemap.xml").mock(
        return_value=httpx.Response(200, text=urlset))
    respx.get("https://careers.fisglobal.com/global/en/sitemap.xml").mock(
        return_value=httpx.Response(200, text=urlset))
    detail = respx.get("https://careers.fisglobal.com/us/en/job/JR1/New-Role").mock(
        return_value=httpx.Response(200, text=_job_page("New Role", "JR1")))
    async with httpx.AsyncClient() as client:
        result = await _fis().fetch(
            client, ConnectorState(last_modified="2026-07-01T00:00:00+00:00"))
    assert len(result.postings) == 1
    assert detail.call_count == 1
