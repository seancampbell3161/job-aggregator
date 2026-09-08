from __future__ import annotations

import html
import re
from datetime import datetime
from typing import Any

import httpx

from src.models import ConnectorState, FetchResult, RawPosting
from src.user_agent import headers as ua_headers

_COMP_RE = re.compile(r"\$([\d,]+)\s*[-–to]+\s*\$([\d,]+)", re.IGNORECASE)


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _strip_html(s: str) -> str:
    s = html.unescape(s)
    s = re.sub(r"<[^>]+>", "", s)
    return re.sub(r"\s+", " ", s).strip()


def _combine_location(j: dict[str, Any]) -> str | None:
    """Greenhouse's `location.name` is often a placeholder ("N/A") while the real
    geography lives in `offices[].name` (e.g. "Canada Locations", "US-Seattle").
    Fold office names into the location string so downstream regexes pick them up."""
    raw = ((j.get("location") or {}).get("name") or "").strip()
    if raw.upper() == "N/A":
        raw = ""
    office_names = [
        (o.get("name") or "").strip()
        for o in (j.get("offices") or [])
        if (o.get("name") or "").strip()
    ]
    parts = [p for p in [raw, *office_names] if p]
    return ", ".join(parts) or None


def _extract_comp(metadata: list[dict[str, Any]]) -> tuple[int | None, int | None]:
    for m in metadata or []:
        v = str(m.get("value") or "")
        match = _COMP_RE.search(v)
        if match:
            lo = int(match.group(1).replace(",", ""))
            hi = int(match.group(2).replace(",", ""))
            return lo, hi
    return None, None


class GreenhouseConnector:
    name: str
    tier: str = "ats"

    def __init__(self, slug: str) -> None:
        self.slug = slug
        self.name = f"greenhouse:{slug}"

    async def fetch(self, client: httpx.AsyncClient, state: ConnectorState) -> FetchResult:
        url = f"https://boards-api.greenhouse.io/v1/boards/{self.slug}/jobs"
        headers: dict[str, str] = ua_headers()
        if state.etag:
            headers["If-None-Match"] = state.etag
        if state.last_modified:
            headers["If-Modified-Since"] = state.last_modified
        resp = await client.get(url, params={"content": "true"}, headers=headers, timeout=20.0)

        if resp.status_code == 304:
            return FetchResult(postings=[], new_state=None, not_modified=True)

        resp.raise_for_status()
        data = resp.json()

        out: list[RawPosting] = []
        for j in data.get("jobs", []):
            posted_at = _parse_dt(j.get("first_published")) or _parse_dt(j.get("updated_at"))
            comp_min, comp_max = _extract_comp(j.get("metadata") or [])
            out.append(
                RawPosting(
                    source=self.name,
                    external_id=str(j["id"]),
                    title=j.get("title") or "",
                    description=_strip_html(j.get("content", "")),
                    apply_url=j.get("absolute_url", ""),
                    location=_combine_location(j),
                    department=(j.get("departments") or [{}])[0].get("name"),
                    posted_at=posted_at,
                    comp_min=comp_min,
                    comp_max=comp_max,
                    raw=j,
                )
            )

        new_state = ConnectorState(
            etag=resp.headers.get("etag"),
            last_modified=resp.headers.get("last-modified"),
        )
        return FetchResult(postings=out, new_state=new_state, not_modified=False)
