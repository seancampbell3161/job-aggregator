from __future__ import annotations

import html
import re
from datetime import datetime
from typing import Any

import httpx

from src.models import ConnectorState, FetchResult, RawPosting
from src.user_agent import headers as ua_headers


def _strip_html(s: str) -> str:
    s = html.unescape(s)
    s = re.sub(r"<[^>]+>", "", s)
    return re.sub(r"\s+", " ", s).strip()


def _description(j: dict[str, Any]) -> str:
    """SmartRecruiters listing payloads sometimes include a jobAd, sometimes not.
    Extract whatever's available; the filter pipeline tolerates an empty description."""
    job_ad = j.get("jobAd") or {}
    sections = (job_ad.get("sections") or {})
    parts: list[str] = []
    for key in ("jobDescription", "qualifications", "additionalInformation"):
        section = sections.get(key) or {}
        text = section.get("text") or ""
        if text:
            parts.append(_strip_html(text))
    return "\n".join(parts)


def _location_text(loc: dict[str, Any]) -> str | None:
    parts = [p for p in [loc.get("city"), loc.get("region"), loc.get("country")] if p]
    return ", ".join(parts) or None


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _apply_url(j: dict[str, Any], slug: str) -> str:
    """Resolve the public apply URL for a posting.

    The listing-level API response does NOT include an ``applyUrl`` field — the
    ``ref`` key is a plain API URL, not a candidate-facing link.  We construct
    the canonical apply URL from the company identifier (pascal-cased slug
    embedded in the payload) and the numeric posting id.  If the company block
    is absent for any reason, fall back to building the URL from the connector
    slug."""
    company = j.get("company") or {}
    identifier = company.get("identifier") or slug
    posting_id = j.get("id") or j.get("uuid") or ""
    if identifier and posting_id:
        return f"https://jobs.smartrecruiters.com/{identifier}/{posting_id}"
    # last resort: return whatever is in ref (the API URL)
    return j.get("ref") or ""


class SmartRecruitersConnector:
    name: str
    tier: str = "ats"

    def __init__(self, slug: str) -> None:
        self.slug = slug
        self.name = f"smartrecruiters:{slug}"

    async def fetch(self, client: httpx.AsyncClient, state: ConnectorState) -> FetchResult:
        url = f"https://api.smartrecruiters.com/v1/companies/{self.slug}/postings"
        headers: dict[str, str] = ua_headers()
        if state.etag:
            headers["If-None-Match"] = state.etag
        if state.last_modified:
            headers["If-Modified-Since"] = state.last_modified
        resp = await client.get(url, params={"limit": 100}, headers=headers, timeout=20.0)

        if resp.status_code == 304:
            return FetchResult(postings=[], new_state=None, not_modified=True)

        resp.raise_for_status()
        data = resp.json()

        out: list[RawPosting] = []
        for j in data.get("content", []):
            loc = j.get("location") or {}
            dept = j.get("department") or {}
            out.append(
                RawPosting(
                    source=self.name,
                    external_id=str(j.get("id") or j.get("uuid") or ""),
                    title=j.get("name") or "",
                    description=_description(j),
                    apply_url=_apply_url(j, self.slug),
                    location=_location_text(loc),
                    remote=loc.get("remote"),
                    department=dept.get("label") if dept else None,
                    posted_at=_parse_dt(j.get("releasedDate")),
                    comp_min=None,
                    comp_max=None,
                    raw=j,
                )
            )

        new_state = ConnectorState(
            etag=resp.headers.get("etag"),
            last_modified=resp.headers.get("last-modified"),
        )
        return FetchResult(postings=out, new_state=new_state, not_modified=False)
