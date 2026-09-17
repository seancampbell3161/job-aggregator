"""JSON-LD scraper connector for SuccessFactors, iCIMS, and TalentBrew.

Recon (2026-07-02) found these three enterprise ATS families serve a static
coarse-listing surface (SF: RSS feed; iCIMS: in_iframe search HTML;
TalentBrew: paginated search HTML) plus a schema.org JobPosting JSON-LD block
on each job's detail page. One connector dispatches on `family` for the
coarse list; enrich() shares the JSON-LD extractor across all three."""
from __future__ import annotations

import html as _htmllib
import json
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import replace
from datetime import datetime, timezone

import httpx

from src.connectors.workday import _strip_html
from src.models import ConnectorState, FetchResult, NormalizedPosting, RawPosting
from src.user_agent import headers as ua_headers

log = logging.getLogger(__name__)

_MAX_DESC = 30000  # match state.posting_display_fields's description_snapshot cap
_MAX_PAGES = 10    # per-board safety cap on HTML pagination
def _headers() -> dict[str, str]:
    return ua_headers()

_LD_SCRIPT_RE = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)

_ICIMS_CARD_RE = re.compile(r'<li class="iCIMS_JobCardItem">(.*?)</li>', re.IGNORECASE | re.DOTALL)
_ICIMS_HREF_RE = re.compile(r'<div class="title">\s*<a href="([^"]+)"', re.IGNORECASE | re.DOTALL)
_ICIMS_TITLE_RE = re.compile(r"<h3[^>]*>(.*?)</h3>", re.IGNORECASE | re.DOTALL)
_ICIMS_LOC_RE = re.compile(r'<div class="header left">\s*<span[^>]*>(.*?)</span>', re.IGNORECASE | re.DOTALL)
_ICIMS_ID_RE = re.compile(r"/jobs/(\d+)/")

_TB_TOTAL_PAGES_RE = re.compile(r'id="search-results"[^>]*data-total-pages="(\d+)"', re.IGNORECASE)
_TB_JOB_RE = re.compile(
    r'<a href="(/[^"]*?/job/[^"]*?/(\d+))"[^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL
)
_TB_TITLE_RE = re.compile(r"<h2[^>]*>(.*?)</h2>", re.IGNORECASE | re.DOTALL)
_TB_LOC_RE = re.compile(r'class="job-location"[^>]*>(.*?)</span>', re.IGNORECASE | re.DOTALL)


def _is_jobposting(obj: object) -> bool:
    if not isinstance(obj, dict):
        return False
    t = obj.get("@type")
    return t == "JobPosting" or (isinstance(t, list) and "JobPosting" in t)


def _jobposting_from_jsonld(html: str) -> dict | None:
    """First schema.org JobPosting dict in any <script type=application/ld+json>
    block. Tolerates a bare object, a list, or an @graph wrapper; returns None
    when none is present or the JSON is malformed."""
    for m in _LD_SCRIPT_RE.finditer(html or ""):
        try:
            data = json.loads(m.group(1).strip())
        except (json.JSONDecodeError, ValueError):
            continue
        candidates = data if isinstance(data, list) else [data]
        for c in candidates:
            if isinstance(c, dict) and isinstance(c.get("@graph"), list):
                candidates = candidates + c["@graph"]
        for c in candidates:
            if _is_jobposting(c):
                return c
    return None


def _sf_posting(self_source: str, company: str | None, item: ET.Element) -> RawPosting | None:
    ns = {"g": "http://base.google.com/ns/1.0"}
    title = (item.findtext("title") or "").strip()
    link = (item.findtext("link") or "").strip()
    gid = (item.findtext("g:id", namespaces=ns) or item.findtext("guid") or "").strip()
    if not (title and link and gid):
        return None
    loc = (item.findtext("g:location", namespaces=ns) or "").strip() or None
    raw_desc = item.findtext("description") or ""
    # SF feed descriptions are double-escaped inside CDATA — one unescape pass.
    desc = _strip_html(_htmllib.unescape(raw_desc))[:_MAX_DESC]
    return RawPosting(
        source=self_source, external_id=gid, title=title, description=desc,
        apply_url=link, location=loc, company=company,
    )


class JsonLdBoardConnector:
    tier = "ats"
    supports_enrich = True

    def __init__(self, family: str, slug: str, base_url: str, company: str | None = None) -> None:
        self.family = family
        self.slug = slug
        self.base_url = base_url.rstrip("/")
        self.company = company
        self.name = f"{family}:{slug}"

    async def fetch(self, client: httpx.AsyncClient, state: ConnectorState) -> FetchResult:
        if self.family == "successfactors":
            postings = await self._fetch_successfactors(client)
        elif self.family == "icims":
            postings = await self._fetch_icims(client)
        elif self.family == "talentbrew":
            postings = await self._fetch_talentbrew(client)
        else:  # defensive — config validation should prevent this
            raise ValueError(f"unknown jsonld family: {self.family}")
        return FetchResult(postings=postings, new_state=None, not_modified=False)

    async def _fetch_successfactors(self, client: httpx.AsyncClient) -> list[RawPosting]:
        text = None
        last_404: httpx.Response | None = None
        for path in ("/jobsfeed.xml", "/sitemap.xml"):
            resp = await client.get(f"{self.base_url}{path}", headers=_headers(), timeout=20.0)
            if resp.status_code == 404:
                last_404 = resp
                continue
            resp.raise_for_status()
            text = resp.text
            break
        if text is None:
            # Both /jobsfeed.xml and /sitemap.xml 404'd — genuine "board is
            # gone" signal. Surface it as an HTTPStatusError (not a bare
            # RequestError) so poll_health.classify_outcome sees a 404 and
            # classifies this as "dead" — a bare RequestError classifies as
            # "transient" and never trips the dead-board suppression.
            assert last_404 is not None
            last_404.raise_for_status()
        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            raise
        items = root.findall(".//item")
        if not items:
            log.info("sf_no_feed", extra={"source": self.name})
            return []
        out = []
        for it in items:
            try:
                p = _sf_posting(self.name, self.company, it)
            except Exception:  # noqa: BLE001 — one bad item never kills the fetch
                continue
            if p is not None:
                out.append(p)
        return out

    async def _fetch_icims(self, client: httpx.AsyncClient) -> list[RawPosting]:
        out: list[RawPosting] = []
        for page in range(_MAX_PAGES):
            url = f"{self.base_url}/jobs/search?ss=1&in_iframe=1&pr={page}&searchRelation=keyword_all"
            try:
                resp = await client.get(url, headers=_headers(), timeout=20.0)
                resp.raise_for_status()
            except Exception:
                if page == 0:
                    raise
                log.warning("icims_page_failed", extra={"source": self.name, "page": page})
                break
            cards = _ICIMS_CARD_RE.findall(resp.text)
            if not cards:
                break  # empty/walled page → done
            for card in cards:
                href_m = _ICIMS_HREF_RE.search(card)
                title_m = _ICIMS_TITLE_RE.search(card)
                if not (href_m and title_m):
                    continue
                href = _htmllib.unescape(href_m.group(1))
                id_m = _ICIMS_ID_RE.search(href)
                if not id_m:
                    continue
                loc_m = _ICIMS_LOC_RE.search(card)
                out.append(RawPosting(
                    source=self.name, external_id=id_m.group(1),
                    title=_strip_html(title_m.group(1)).strip(),
                    description="", apply_url=href,
                    location=(_strip_html(loc_m.group(1)).strip() if loc_m else None),
                    company=self.company,
                ))
        return out

    async def _fetch_talentbrew(self, client: httpx.AsyncClient) -> list[RawPosting]:
        out: list[RawPosting] = []
        total_pages = 1
        for page in range(1, _MAX_PAGES + 1):
            url = f"{self.base_url}/search-jobs?p={page}"
            try:
                resp = await client.get(url, headers=_headers(), timeout=20.0)
                resp.raise_for_status()
            except Exception:
                if page == 1:
                    raise
                log.warning("talentbrew_page_failed", extra={"source": self.name, "page": page})
                break
            if page == 1:
                tp = _TB_TOTAL_PAGES_RE.search(resp.text)
                total_pages = min(int(tp.group(1)), _MAX_PAGES) if tp else 1
            for href, jid, inner in _TB_JOB_RE.findall(resp.text):
                title_m = _TB_TITLE_RE.search(inner)
                if not title_m:
                    continue
                loc_m = _TB_LOC_RE.search(inner)
                out.append(RawPosting(
                    source=self.name, external_id=jid,
                    title=_strip_html(title_m.group(1)).strip(),
                    description="", apply_url=f"{self.base_url}{_htmllib.unescape(href)}",
                    location=(_strip_html(loc_m.group(1)).strip() if loc_m else None),
                    company=self.company,
                ))
            if page >= total_pages:
                break
        return out

    async def enrich(self, client: httpx.AsyncClient, posting: NormalizedPosting) -> NormalizedPosting:
        """GET the detail page, swap in the JSON-LD description. iCIMS detail
        JSON-LD only renders with in_iframe=1. Best-effort: any failure returns
        the posting unchanged."""
        url = posting.apply_url
        if self.family == "icims" and "in_iframe=1" not in url:
            url = url + ("&" if "?" in url else "?") + "in_iframe=1"
        try:
            resp = await client.get(url, headers=_headers(), timeout=20.0)
            resp.raise_for_status()
            jp = _jobposting_from_jsonld(resp.text)
        except Exception as exc:  # noqa: BLE001 — enrich is best-effort
            log.warning("jsonld_enrich_failed",
                        extra={"source": self.name, "job_id": posting.job_id, "error": str(exc)})
            return posting
        if not jp:
            return posting
        desc = _strip_html(str(jp.get("description") or ""))[:_MAX_DESC]
        if not desc:
            return posting
        return replace(posting, description=desc)
