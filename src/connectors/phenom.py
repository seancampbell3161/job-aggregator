"""Phenom People career-site connector.

Phenom hosts branded career sites on each company's own domain
(careers.fisglobal.com, jobs.gehealthcare.com, …). The site publishes a
static sitemap of job URLs + <lastmod>, and each job page embeds a
schema.org JobPosting JSON-LD block — but the sitemap carries no titles, so
fetch() must pull each job page to know what it is. To stay polite on a
300–4000-job board, fetch() diffs the sitemap against a persisted lastmod
watermark and fetches detail only for jobs newer than the watermark (minus a
30-min overlap). supports_enrich is False — the detail fetch already carries
the full description."""
from __future__ import annotations

import asyncio
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import httpx

from src.connectors.jsonld import _MAX_DESC, _jobposting_from_jsonld
from src.connectors.workday import _strip_html
from src.models import ConnectorState, FetchResult, RawPosting
from src.user_agent import headers as ua_headers

log = logging.getLogger(__name__)

_FIRST_RUN_DAYS = 3
_MAX_JOBS_PER_RUN = 100
_OVERLAP = timedelta(minutes=30)
_MAX_SUB_SITEMAPS = 20
_DETAIL_CONCURRENCY = 4
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
def _headers() -> dict[str, str]:
    return ua_headers()

_SITEMAP_DIRECTIVE_RE = re.compile(r"(?im)^\s*Sitemap:\s*(\S+)\s*$")
_SM_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"


def _slug(careers_url: str) -> str:
    host = (urlparse(careers_url).netloc or careers_url).lower()
    for prefix in ("careers.", "jobs.", "www."):
        if host.startswith(prefix):
            host = host[len(prefix):]
            break
    return host.replace(".", "-")


def _sitemap_roots_from_robots(robots_text: str, careers_url: str) -> list[str]:
    roots = _SITEMAP_DIRECTIVE_RE.findall(robots_text or "")
    if roots:
        if len(roots) > _MAX_SUB_SITEMAPS:
            log.debug("phenom_robots_sitemap_roots_truncated",
                      extra={"careers_url": careers_url, "total": len(roots),
                             "cap": _MAX_SUB_SITEMAPS})
        return roots[:_MAX_SUB_SITEMAPS]
    return [f"{careers_url.rstrip('/')}/sitemap.xml"]


def _parse_sitemap(xml_text: str) -> tuple[str, list]:
    """('index', [child_loc,…]) for a <sitemapindex>, or
    ('urlset', [(loc, lastmod_or_None),…]) filtered to /job/ locs, for a
    <urlset>. Raises ET.ParseError on unparseable XML."""
    root = ET.fromstring(xml_text)
    tag = root.tag.split("}")[-1]
    if tag == "sitemapindex":
        locs = [
            (sm.findtext(f"{_SM_NS}loc") or "").strip()
            for sm in root.findall(f"{_SM_NS}sitemap")
        ]
        return "index", [loc for loc in locs if loc]
    entries: list[tuple[str, str | None]] = []
    for url in root.findall(f"{_SM_NS}url"):
        loc = (url.findtext(f"{_SM_NS}loc") or "").strip()
        if "/job/" not in loc:
            continue
        lastmod = (url.findtext(f"{_SM_NS}lastmod") or "").strip() or None
        entries.append((loc, lastmod))
    return "urlset", entries


def _parse_lastmod(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _parse_posted(s: str | None) -> datetime | None:
    return _parse_lastmod(s)  # identical ISO-date/datetime + UTC-stamp rule


def _jsonld_identifier(jp: dict, url: str) -> str:
    ident = jp.get("identifier")
    if isinstance(ident, dict):
        ident = ident.get("value")
    if ident:
        return str(ident).strip()
    # Fallback: the path segment right after "/job/".
    segs = [s for s in urlparse(url).path.split("/") if s]
    if "job" in segs:
        i = segs.index("job")
        if i + 1 < len(segs):
            return segs[i + 1]
    return segs[-1] if segs else url


def _jsonld_location(jp: dict) -> str | None:
    loc = jp.get("jobLocation")
    if isinstance(loc, list):
        loc = loc[0] if loc else None
    if not isinstance(loc, dict):
        return None
    addr = loc.get("address")
    if not isinstance(addr, dict):
        return None
    parts = [addr.get("addressLocality"), addr.get("addressRegion"), addr.get("addressCountry")]
    out = ", ".join(str(p) for p in parts if p)
    return out or None


class PhenomConnector:
    tier = "ats"
    supports_enrich = False

    def __init__(self, careers_url: str, company: str | None = None) -> None:
        self.careers_url = careers_url.rstrip("/")
        self.company = company
        self.name = f"phenom:{_slug(careers_url)}"

    async def _get_text(self, client: httpx.AsyncClient, url: str) -> str:
        resp = await client.get(url, headers=_headers(), timeout=20.0)
        resp.raise_for_status()
        return resp.text

    async def _collect_job_entries(
        self, client: httpx.AsyncClient, roots: list[str], fallback: str
    ) -> list[tuple[str, str | None]]:
        """Walk the sitemap tree from the root URLs. If none of `roots` yields
        any sitemap document, also try `fallback` once (unless it was already
        among `roots`) before giving up — a stale robots.txt Sitemap:
        directive must not permanently kill a live board when the plain
        /sitemap.xml still works. Raises the last error only if NEITHER the
        advertised roots NOR the fallback yielded any sitemap document (the
        'board is gone' signal)."""
        entries: list[tuple[str, str | None]] = []
        got_any = False
        last_err: Exception | None = None

        async def _try_root(root: str) -> None:
            nonlocal got_any, last_err
            try:
                text = await self._get_text(client, root)
            except Exception as exc:  # noqa: BLE001 — try the next root
                last_err = exc
                return
            got_any = True
            try:
                kind, items = _parse_sitemap(text)
            except ET.ParseError:
                return
            if kind == "urlset":
                entries.extend(items)
                return
            # index → fetch children (capped)
            children = items[:_MAX_SUB_SITEMAPS]
            if len(items) > _MAX_SUB_SITEMAPS:
                log.warning("phenom_sitemap_index_truncated",
                            extra={"source": self.name, "total": len(items), "cap": _MAX_SUB_SITEMAPS})
            for child in children:
                try:
                    ctext = await self._get_text(client, child)
                    ckind, citems = _parse_sitemap(ctext)
                except Exception:  # noqa: BLE001 — skip a bad sub-sitemap
                    continue
                if ckind == "urlset":
                    entries.extend(citems)

        for root in roots:
            await _try_root(root)
        if not got_any and fallback not in roots:
            await _try_root(fallback)
        if not got_any and last_err is not None:
            raise last_err
        return entries

    async def _fetch_detail(self, client: httpx.AsyncClient, url: str) -> RawPosting | None:
        """Best-effort: any failure or missing JSON-LD returns None."""
        try:
            text = await self._get_text(client, url)
            jp = _jobposting_from_jsonld(text)
        except Exception:  # noqa: BLE001 — one bad job never fails the sweep
            log.debug("phenom_detail_failed", extra={"source": self.name, "url": url})
            return None
        if not jp:
            return None
        title = str(jp.get("title") or "").strip()
        if not title:
            return None
        return RawPosting(
            source=self.name,
            external_id=_jsonld_identifier(jp, url),
            title=title,
            description=_strip_html(str(jp.get("description") or ""))[:_MAX_DESC],
            apply_url=url,
            location=_jsonld_location(jp),
            company=self.company,
            posted_at=_parse_posted(jp.get("datePosted")),
        )

    async def fetch(self, client: httpx.AsyncClient, state: ConnectorState) -> FetchResult:
        # 1. discover sitemap roots (robots 404 → fallback; never raises here)
        try:
            robots = await self._get_text(client, f"{self.careers_url}/robots.txt")
        except Exception:  # noqa: BLE001 — no robots is normal
            robots = ""
        roots = _sitemap_roots_from_robots(robots, self.careers_url)
        # 2. walk the tree (raises only if roots AND the /sitemap.xml fallback
        # all fail — a stale robots.txt directive alone must not kill a live
        # board)
        fallback = f"{self.careers_url}/sitemap.xml"
        entries = await self._collect_job_entries(client, roots, fallback)
        # de-dupe: multi-locale boards can list the same canonical job loc in
        # more than one sitemap
        seen_locs: set[str] = set()
        entries = [(loc, lm) for loc, lm in entries
                   if not (loc in seen_locs or seen_locs.add(loc))]
        if not entries:
            log.warning("phenom_no_job_entries", extra={"source": self.name})
            return FetchResult(postings=[], new_state=None, not_modified=False)
        # 3. diff by watermark
        now = datetime.now(timezone.utc)
        watermark = _parse_lastmod(state.last_modified)
        since = (watermark - _OVERLAP) if watermark else (now - timedelta(days=_FIRST_RUN_DAYS))
        parsed = [(loc, _parse_lastmod(lm)) for loc, lm in entries]
        selected = [(loc, lm) for loc, lm in parsed if (lm or _EPOCH) >= since]
        selected.sort(key=lambda e: e[1] or _EPOCH, reverse=True)
        if len(selected) > _MAX_JOBS_PER_RUN:
            log.warning("phenom_truncated",
                        extra={"source": self.name, "dropped": len(selected) - _MAX_JOBS_PER_RUN,
                               "cap": _MAX_JOBS_PER_RUN})
            selected = selected[:_MAX_JOBS_PER_RUN]
        # 4. fetch detail concurrently, best-effort
        sem = asyncio.Semaphore(_DETAIL_CONCURRENCY)

        async def _one(loc: str) -> RawPosting | None:
            async with sem:
                return await self._fetch_detail(client, loc)

        results = await asyncio.gather(*(_one(loc) for loc, _ in selected))
        postings = [p for p in results if p is not None]
        # 5. advance watermark to max lastmod across ALL entries — but never
        # regress below the prior watermark (a delisted newest job, or a
        # transiently-failed sub-sitemap, can make max(all_lm) older than
        # what we already persisted)
        all_lm = [lm for _, lm in parsed if lm is not None]
        candidates = [lm for lm in all_lm]
        if watermark is not None:
            candidates.append(watermark)
        new_wm = max(candidates) if candidates else None
        new_state = ConnectorState(last_modified=new_wm.isoformat()) if new_wm else None
        return FetchResult(postings=postings, new_state=new_state, not_modified=False)
