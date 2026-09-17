"""Workday job-board connector.

Workday companies expose a per-tenant CXS API at
``https://<tenant>.<region>.myworkdayjobs.com/wday/cxs/<tenant>/<site>/jobs``,
where ``region`` is the Workday cluster (wd1, wd5, wd12, ...) and ``site`` is
the named board (External, External_Career_Site, NVIDIAExternalCareerSite, ...).
A single tenant can expose multiple sites; we treat each site as its own
connector.

The list endpoint returns coarse fields (title, externalPath, locationsText,
postedOn text, bulletFields with the JR-prefixed job id). Structured location
data lives behind a follow-up GET on the externalPath; we don't fetch that
today because it would N×-multiply per-cycle request count. Multi-location
postings (``locationsText='6 Locations'``) currently fail the location filter
unless allow_unknown is set — that's an acceptable precision/recall trade for a
personal tool.
"""

from __future__ import annotations

import html as _html
import logging
import re
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from bs4 import BeautifulSoup

from src.models import ConnectorState, FetchResult, NormalizedPosting, RawPosting
from src.user_agent import headers as ua_headers

log = logging.getLogger(__name__)

_DAYS_AGO_RE = re.compile(r"posted\s+(\d+)\+?\s+day", re.IGNORECASE)
_POSTED_TODAY = ("posted today", "posted yesterday")

_PAGE_SIZE = 20      # Workday 400s on limit >= 50; its UI default is 20
# Cap pages low: Workday sorts newest-first, so with filters.max_age_days small
# (e.g. 2) the only roles that survive are on the first page or two. 5 pages
# (~100 newest roles/board) covers that window while keeping the per-cycle fetch
# cheap — big boards (CVS 16k, Lowe's 12k) otherwise paginated to ~1000 each and
# pushed the ats cycle past 100s. Raise this only alongside relaxing
# max_age_days AND moving Workday to a slower tier.
_MAX_PAGES = 5       # safety + cost cap → <= 100 roles/board/cycle
def _headers() -> dict[str, str]:
    # Verified 2026-09-08 from a residential IP: salesforce/paypal tenants
    # answer 200 to the default UA. Workday has historically 400'd unfamiliar
    # clients from datacenter IPs — set http.user_agent if a deployment hits
    # that.
    return ua_headers(Accept="application/json")


_MAX_DESC = 30000  # match SeenJobsStore.claim_for_notify's description_snapshot cap


def _strip_html(s: str) -> str:
    """HTML → collapsed plain text. Mirrors src/connectors/hn.py's approach."""
    soup = BeautifulSoup(_html.unescape(s or ""), "html.parser")
    return re.sub(r"\s+", " ", soup.get_text(separator=" ")).strip()


def _parse_posted_on(text: str | None) -> datetime | None:
    """Map Workday's human-text postedOn field to a UTC datetime.

    Recognised inputs: "Posted Today", "Posted Yesterday", "Posted N Days Ago",
    "Posted 30+ Days Ago". Anything else returns None — the age filter then
    treats it as UNKNOWN."""
    if not text:
        return None
    s = text.strip().lower()
    now = datetime.now(timezone.utc)
    if any(s.startswith(p) for p in _POSTED_TODAY):
        return now
    m = _DAYS_AGO_RE.search(s)
    if m:
        return now - timedelta(days=int(m.group(1)))
    return None


def _external_id(j: dict[str, Any]) -> str:
    """Stable per-posting id. Prefer the JR-prefixed value in bulletFields[0]
    (Workday's canonical external id); fall back to the trailing path segment."""
    bullets = j.get("bulletFields") or []
    if bullets and bullets[0]:
        return str(bullets[0])
    path = j.get("externalPath") or ""
    return path.rsplit("/", 1)[-1] if path else j.get("title", "")


class WorkdayConnector:
    name: str
    tier: str = "ats"
    supports_enrich = True

    def __init__(self, tenant: str, region: str, site: str) -> None:
        self.tenant = tenant
        self.region = region
        self.site = site
        self.name = f"workday:{tenant}:{site}"

    @property
    def _base(self) -> str:
        return f"https://{self.tenant}.{self.region}.myworkdayjobs.com"

    def _to_raw(self, j: dict[str, Any]) -> RawPosting:
        # externalPath is site-relative (e.g. "/job/Remote---USA/Role_JR1"); the
        # public URL must include the career-site segment or Workday serves a
        # generic "page not found" (the site-less "{base}{externalPath}" form
        # 404s). This mirrors the API's own jobPostingInfo.externalUrl, which is
        # "{base}/{site}{externalPath}". enrich()/closed_check strip the leading
        # "/{site}" back off to rebuild the CXS detail path.
        external_path = j.get("externalPath") or ""
        apply_url = f"{self._base}/{self.site}{external_path}" if external_path else self._base
        return RawPosting(
            source=self.name,
            external_id=_external_id(j),
            title=j.get("title") or "",
            description=" | ".join(j.get("bulletFields") or []),
            apply_url=apply_url,
            location=j.get("locationsText"),
            remote=None,
            department=None,
            posted_at=_parse_posted_on(j.get("postedOn")),
            raw=j,
        )

    async def fetch(self, client: httpx.AsyncClient, state: ConnectorState) -> FetchResult:
        # Workday's POST endpoint ignores If-None-Match; state.etag is unused and
        # each cycle pays full POSTs. Paginate via offset until a partial/empty
        # page, the reported total, or the safety cap.
        url = f"{self._base}/wday/cxs/{self.tenant}/{self.site}/jobs"
        out: list[RawPosting] = []
        offset = 0
        total: int | None = None
        for _ in range(_MAX_PAGES):
            try:
                resp = await client.post(
                    url,
                    json={"appliedFacets": {}, "limit": _PAGE_SIZE, "offset": offset, "searchText": ""},
                    headers=_headers(),
                    timeout=20.0,
                )
                resp.raise_for_status()
                data = resp.json()
            except Exception:
                if offset == 0:
                    raise  # first-page failure → connector failed this cycle (as before)
                log.warning("workday_page_failed", extra={"source": self.name, "offset": offset})
                break  # later-page failure → return what we have
            postings = data.get("jobPostings", [])
            if not postings:
                break
            out.extend(self._to_raw(j) for j in postings)
            if len(postings) < _PAGE_SIZE:
                break  # partial page → last page
            total = data.get("total", total)
            offset += _PAGE_SIZE
            if total is not None and offset >= total:
                break
        return FetchResult(postings=out, new_state=None, not_modified=False)

    async def enrich(
        self, client: httpx.AsyncClient, posting: NormalizedPosting
    ) -> NormalizedPosting:
        """Fetch the per-posting detail JD and return a copy with the real
        description + combined location. Best-effort: any failure returns the
        posting unchanged (the caller keeps the coarse list data)."""
        if not posting.apply_url.startswith(self._base):
            return posting
        # apply_url is "{base}/{site}{externalPath}"; strip the leading "/{site}"
        # to recover the site-relative externalPath the CXS detail endpoint wants,
        # else the site segment is doubled. Tolerate legacy rows stored before the
        # site prefix was added (path already bare "{externalPath}").
        path = posting.apply_url[len(self._base):]
        site_prefix = f"/{self.site}"
        external_path = path[len(site_prefix):] if path.startswith(site_prefix + "/") else path
        detail_url = f"{self._base}/wday/cxs/{self.tenant}/{self.site}{external_path}"
        try:
            resp = await client.get(detail_url, headers=_headers(), timeout=20.0)
            resp.raise_for_status()
            info = (resp.json() or {}).get("jobPostingInfo") or {}
        except Exception as exc:  # noqa: BLE001 — enrich is best-effort
            log.warning(
                "workday_enrich_failed",
                extra={"source": self.name, "job_id": posting.job_id, "error": str(exc)},
            )
            return posting

        desc = _strip_html(info.get("jobDescription") or "")
        if not desc:
            return posting

        locs: list[str | None] = [info.get("location")]
        for a in info.get("additionalLocations") or []:
            locs.append(a if isinstance(a, str) else (a.get("name") if isinstance(a, dict) else None))
        location_text = "; ".join(x for x in locs if x) or posting.location_text

        return replace(posting, description=desc[:_MAX_DESC], location_text=location_text)
