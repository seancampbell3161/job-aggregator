"""Modern Oracle Taleo career-section connector.

Modern Taleo tenants expose an unauthenticated JSON search API at
``/careersection/rest/jobboard/searchjobs`` — but it 500s unless the request
carries the tz/tzname headers AND the full facet-selection body (verified via
live recon 2026-07-03 on textron + cinfin). The listing has no plain-HTML
fallback. Job detail is server-rendered inside a ``fillList`` JS call on
jobdetail.ftl (no XHR). The response ``column`` order varies per tenant, so
fields are identified by shape, not index."""
from __future__ import annotations

import html as _htmllib
import json
import logging
import re
from dataclasses import replace
from datetime import datetime, timezone
from urllib.parse import unquote

import httpx

from src.connectors.jsonld import _MAX_DESC
from src.connectors.workday import _strip_html
from src.models import ConnectorState, FetchResult, NormalizedPosting, RawPosting
from src.user_agent import headers as ua_headers

log = logging.getLogger(__name__)

_MAX_PAGES = 10
def _headers() -> dict[str, str]:
    # Taleo requires tz/tzname present or searchjobs 500s. Values are
    # display-only (candidate timezone) and don't affect results.
    return ua_headers(
        **{
            "Content-Type": "application/json",
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/plain, */*",
            "tz": "GMT-05:00",
            "tzname": "America/Chicago",
        }
    )

_PORTAL_RE = re.compile(r"portalNo['\":\s]+(\d+)")
_LOCATION_RE = re.compile(r'^\[".*"\]$', re.DOTALL)      # JSON array, e.g. ["OH-Cincinnati"]
_DATE_RE = re.compile(r"^\d{1,2}/\d{1,2}/\d{4}$")
_FILLLIST_RE = re.compile(
    r"fillList\(\s*'requisitionDescriptionInterface'\s*,\s*'descRequisition'\s*,\s*\[",
    re.DOTALL,
)
_JS_STR_RE = re.compile(r"'((?:[^'\\]|\\.)*)'", re.DOTALL)


def _extract_description(html: str) -> str | None:
    """Decode the Description from a Taleo jobdetail.ftl `fillList` blob: find
    the array, take the first element starting with the `!*!` rich-text marker,
    JS-unescape → percent-decode → HTML-unescape → strip. None if absent."""
    m = _FILLLIST_RE.search(html or "")
    if not m:
        return None
    # scan from the '[' for its matching ']', string-aware (values contain ']')
    start = m.end() - 1
    i, depth, in_str, esc = start + 1, 1, False, False
    while i < len(html) and depth > 0:
        ch = html[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == "'":
                in_str = False
        elif ch == "'":
            in_str = True
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
        i += 1
    inner = html[start + 1:i - 1]
    for raw in _JS_STR_RE.findall(inner):
        if not raw.startswith("!*!"):
            continue
        val = raw[3:]
        val = (val.replace("\\'", "'").replace('\\"', '"')
                  .replace("\\/", "/").replace("\\\\", "\\"))
        val = unquote(val)
        val = _htmllib.unescape(val)
        return _strip_html(val)[:_MAX_DESC] or None
    return None


def _search_body(page_no: int) -> dict:
    return {
        "multilineEnabled": False,
        "sortingSelection": {"sortBySelectionParam": "3", "ascendingSortingOrder": "false"},
        "fieldData": {"fields": {"KEYWORD": "", "LOCATION": ""}, "valid": True},
        "filterSelectionParam": {"searchFilterSelections": [
            {"id": "POSTING_DATE", "selectedValues": []},
            {"id": "LOCATION", "selectedValues": []},
            {"id": "JOB_FIELD", "selectedValues": []},
            {"id": "JOB_TYPE", "selectedValues": []},
            {"id": "JOB_SCHEDULE", "selectedValues": []},
            {"id": "JOB_LEVEL", "selectedValues": []},
        ]},
        "advancedSearchFiltersSelectionParam": {"searchFilterSelections": [
            {"id": "ORGANIZATION", "selectedValues": []},
            {"id": "LOCATION", "selectedValues": []},
            {"id": "JOB_FIELD", "selectedValues": []},
            {"id": "JOB_NUMBER", "selectedValues": []},
            {"id": "URGENT_JOB", "selectedValues": []},
            {"id": "EMPLOYEE_STATUS", "selectedValues": []},
            {"id": "WILL_TRAVEL", "selectedValues": []},
            {"id": "JOB_SHIFT", "selectedValues": []},
        ]},
        "pageNo": page_no,
    }


def _parse_location(s: str) -> str | None:
    try:
        arr = json.loads(s)
        if isinstance(arr, list):
            return "; ".join(str(x) for x in arr if x) or None
    except (json.JSONDecodeError, ValueError):
        pass
    return s.strip('[]"') or None


def _parse_mdy(s: str) -> datetime | None:
    try:
        return datetime.strptime(s, "%m/%d/%Y").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _classify_columns(
    columns: list, contest_no: str
) -> tuple[str | None, str | None, datetime | None]:
    """Identify (title, location, posted_at) from a requisition's `column` list
    by SHAPE — the order varies per tenant. Skip the entry equal to contestNo;
    a JSON-array entry is the location; an MM/DD/YYYY entry is the date; the
    first remaining free-text entry is the title."""
    title = location = None
    posted = None
    for c in columns:
        s = str(c or "").strip()
        if not s or s == contest_no:
            continue
        if _DATE_RE.match(s):
            posted = _parse_mdy(s)
            continue
        if _LOCATION_RE.match(s):
            if location is None:
                location = _parse_location(s)
            continue
        if title is None:
            title = s
    return title, location, posted


class TaleoConnector:
    tier = "ats"
    supports_enrich = True

    def __init__(self, tenant: str, section: str, company: str | None = None) -> None:
        self.tenant = tenant
        self.section = section
        self.company = company
        self.name = f"taleo:{tenant}:{section}"

    @property
    def _base(self) -> str:
        return f"https://{self.tenant}.taleo.net/careersection/{self.section}"

    def _to_raw(self, req: dict) -> RawPosting | None:
        contest_no = str(req.get("contestNo") or "").strip()
        if not contest_no:
            return None
        title, location, posted = _classify_columns(req.get("column") or [], contest_no)
        if not title:
            return None
        return RawPosting(
            source=self.name, external_id=contest_no, title=title, description="",
            apply_url=f"{self._base}/jobdetail.ftl?job={contest_no}",
            location=location, company=self.company, posted_at=posted, raw=req,
        )

    async def fetch(self, client: httpx.AsyncClient, state: ConnectorState) -> FetchResult:
        # 1. GET jobsearch.ftl → session cookie + portalNo
        resp = await client.get(f"{self._base}/jobsearch.ftl?lang=en", headers=_headers(), timeout=20.0)
        resp.raise_for_status()
        m = _PORTAL_RE.search(resp.text)
        if not m:
            raise httpx.RequestError(f"taleo portalNo not found for {self.name}")
        portal = m.group(1)
        url = (f"https://{self.tenant}.taleo.net/careersection/rest/jobboard/searchjobs"
               f"?lang=en&portal={portal}")
        # 2. POST searchjobs, paginate
        out: list[RawPosting] = []
        seen: set[str] = set()
        for page in range(1, _MAX_PAGES + 1):
            try:
                r = await client.post(url, headers=_headers(), json=_search_body(page), timeout=20.0)
                r.raise_for_status()
                data = r.json()
            except Exception:
                if page == 1:
                    raise  # first-page failure → connector failed this cycle
                log.warning("taleo_page_failed", extra={"source": self.name, "page": page})
                break
            reqs = data.get("requisitionList") or []
            if not reqs:
                break
            for req in reqs:
                raw = self._to_raw(req)
                if raw is None or raw.external_id in seen:
                    continue
                seen.add(raw.external_id)
                out.append(raw)
            paging = data.get("pagingData") or {}
            total = paging.get("totalCount")
            size = paging.get("pageSize") or 25
            if total is not None and page * size >= int(total):
                break
        return FetchResult(postings=out, new_state=None, not_modified=False)

    async def enrich(self, client: httpx.AsyncClient, posting: NormalizedPosting) -> NormalizedPosting:
        try:
            resp = await client.get(posting.apply_url, headers=_headers(), timeout=20.0)
            resp.raise_for_status()
            desc = _extract_description(resp.text)
        except Exception as exc:  # noqa: BLE001 — enrich is best-effort
            log.warning("taleo_enrich_failed",
                        extra={"source": self.name, "job_id": posting.job_id, "error": str(exc)})
            return posting
        if not desc:
            return posting
        return replace(posting, description=desc)
