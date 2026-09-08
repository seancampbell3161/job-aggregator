from __future__ import annotations

import pytest

from src.connectors.avature import AvatureConnector, parse_avature_results
from src.models import ConnectorState, NormalizedPosting

# Avature's shared careers theme — selectors confirmed live 2026-07-03. (That a
# HEADLESS browser clears Avature's bot-challenge is a separate rollout gate—see spec.)
# article.article--result cards, title = heading <a>, location = span.list-item-location.
_SEARCHJOBS_HTML = """
<div class="results__panel">
  <article class="article article--result">
    <h3><a href="/en_US/careers/JobDetail/senior-software-engineer/48213">Senior Software Engineer</a></h3>
    <span class="list-item-location">Dallas, TX, United States</span>
  </article>
  <article class="article article--result">
    <h3><a href="/en_US/careers/JobDetail/data-scientist/48999">Data Scientist</a></h3>
    <span class="list-item-location">Remote, US</span>
  </article>
  <article class="article article--result">
    <h3><a href="/en_US/careers/JobDetail/untitled/50000"></a></h3>
    <span class="list-item-location">Nowhere</span>
  </article>
</div>
"""


def test_parse_avature_extracts_cards():
    posts = parse_avature_results(
        _SEARCHJOBS_HTML, base_url="https://careers.jacobs.com",
        company="Jacobs", source="avature:jacobs",
    )
    # the empty-title card is skipped → 2 postings
    assert [p.title for p in posts] == ["Senior Software Engineer", "Data Scientist"]
    p = posts[0]
    assert p.source == "avature:jacobs"
    assert p.external_id == "48213"                      # trailing numeric id
    assert p.apply_url == "https://careers.jacobs.com/en_US/careers/JobDetail/senior-software-engineer/48213"
    assert p.location == "Dallas, TX, United States"
    assert p.company == "Jacobs"


def test_avature_connector_name_from_host():
    c = AvatureConnector("https://careers.jacobs.com/en_US/careers/SearchJobs", "Jacobs")
    assert c.name == "avature:jacobs"
    assert c.tier == "headless"
    assert c.supports_enrich is True


class _FakePage:
    """Minimal async stand-in for a Playwright page — no real browser."""
    def __init__(self, html: str, *, raise_on_wait: bool = False):
        self._html = html
        self._raise = raise_on_wait
        self.goto_urls: list[str] = []
    async def goto(self, url, **kw):
        self.goto_urls.append(url)
    async def wait_for_selector(self, selector, **kw):
        if self._raise:
            raise RuntimeError("selector never appeared")
    async def query_selector_all(self, selector):
        return ["card", "card"]      # stable count → loop breaks after 2 samples
    async def wait_for_timeout(self, ms):
        return None
    async def content(self):
        return self._html


@pytest.mark.asyncio
async def test_fetch_renders_and_parses():
    conn = AvatureConnector("https://careers.jacobs.com/en_US/careers/SearchJobs", "Jacobs")
    page = _FakePage(_SEARCHJOBS_HTML)
    result = await conn.fetch(page, ConnectorState())
    assert {p.title for p in result.postings} == {"Senior Software Engineer", "Data Scientist"}
    assert page.goto_urls == ["https://careers.jacobs.com/en_US/careers/SearchJobs"]


@pytest.mark.asyncio
async def test_fetch_raises_when_list_never_renders():
    conn = AvatureConnector("https://careers.jacobs.com/en_US/careers/SearchJobs", "Jacobs")
    page = _FakePage("<html>challenge</html>", raise_on_wait=True)
    with pytest.raises(Exception):
        await conn.fetch(page, ConnectorState())


@pytest.mark.asyncio
async def test_enrich_uses_jsonld_description_when_present():
    conn = AvatureConnector("https://careers.jacobs.com/en_US/careers/SearchJobs", "Jacobs")
    jd_html = ('<html><script type="application/ld+json">'
               '{"@type":"JobPosting","title":"Senior Software Engineer",'
               '"description":"Build reliable distributed systems in Python."}'
               '</script></html>')
    page = _FakePage(jd_html)
    posting = NormalizedPosting(
        job_id="avature:jacobs:48213", title="Senior Software Engineer", company="Jacobs",
        location_text="Dallas, TX", location_tags=frozenset(), seniority="senior",
        stack=frozenset(), comp_min=None, comp_max=None,
        apply_url="https://careers.jacobs.com/en_US/careers/JobDetail/x/48213",
        description="", posted_at=None, source="avature:jacobs")
    out = await conn.enrich(page, posting)
    assert "distributed systems" in out.description


@pytest.mark.asyncio
async def test_enrich_unchanged_when_no_description():
    conn = AvatureConnector("https://careers.jacobs.com/en_US/careers/SearchJobs", "Jacobs")
    page = _FakePage("<html><body>no jsonld here</body></html>")
    posting = NormalizedPosting(
        job_id="avature:jacobs:1", title="T", company="Jacobs", location_text="x",
        location_tags=frozenset(), seniority=None, stack=frozenset(), comp_min=None,
        comp_max=None, apply_url="https://careers.jacobs.com/x", description="orig",
        posted_at=None, source="avature:jacobs")
    out = await conn.enrich(page, posting)
    assert out.description == "orig"


def test_job_external_id_numeric_and_fallback():
    from src.connectors.avature import _job_external_id
    # canonical Avature shape: /JobDetail/<slug>/<id> → trailing numeric run
    assert _job_external_id("/en_US/careers/JobDetail/senior-engineer/48213") == "48213"
    # multiple numeric runs → the LAST one (Avature puts the id last)
    assert _job_external_id("/careers/JobDetail/eng-2028/48213") == "48213"
    # no numeric run → last path segment
    assert _job_external_id("/careers/JobDetail/some-slug") == "some-slug"


def test_extract_jd_region_fallback_when_no_jsonld():
    from src.connectors.avature import _extract_jd
    html = ('<html><body>'
            '<div class="job-details">We build reliable Python services at scale.</div>'
            '</body></html>')
    desc = _extract_jd(html)
    assert desc is not None
    assert "reliable Python services" in desc
