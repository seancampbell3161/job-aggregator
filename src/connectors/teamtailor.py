"""Teamtailor — public per-tenant RSS job feed (Nordics leader).

GET https://{slug}.teamtailor.com/jobs.rss, no authentication (the JSON API
needs a key; the RSS carries everything we need). Custom XML namespace
https://teamtailor.com/locations provides structured city/country as
ATTRIBUTES of <tt:location>; <remoteStatus> is a plain un-namespaced element
with values like "fully"/"hybrid"/"none". Items may lack <pubDate>
(posted_at=None rides filter_age's UNKNOWN path)."""

from __future__ import annotations

import email.utils
import html
import re
import xml.etree.ElementTree as ET
from datetime import datetime

import httpx

from src.connectors.workplace import annotate_location, remote_from_workplace_type
from src.models import ConnectorState, FetchResult, RawPosting
from src.user_agent import headers as ua_headers

_TT_NS = "{https://teamtailor.com/locations}"

# Teamtailor remoteStatus vocabulary → the workplace-type strings
# remote_from_workplace_type understands. Unmapped values (e.g. "temporary")
# fall through to None → remote flag stays None.
_REMOTE_STATUS_TO_WORKPLACE = {
    "fully": "remote",
    "fully_remote": "remote",
    "remote": "remote",
    "hybrid": "hybrid",
    "none": "onsite",
    "onsite": "onsite",
}


def _strip_html(s: str) -> str:
    s = html.unescape(s)
    s = re.sub(r"<[^>]+>", "", s)
    return re.sub(r"\s+", " ", s).strip()


def _parse_pubdate(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return email.utils.parsedate_to_datetime(s)
    except (TypeError, ValueError):
        return None


def _location_text(item: ET.Element) -> str | None:
    parts: list[str] = []
    for loc in item.findall(f"{_TT_NS}locations/{_TT_NS}location"):
        piece = ", ".join(p for p in (loc.get("city"), loc.get("country")) if p)
        if piece:
            parts.append(piece)
    return "; ".join(parts) or None


class TeamtailorConnector:
    name: str
    tier: str = "ats"

    def __init__(self, slug: str) -> None:
        self.slug = slug
        self.name = f"teamtailor:{slug}"

    async def fetch(self, client: httpx.AsyncClient, state: ConnectorState) -> FetchResult:
        url = f"https://{self.slug}.teamtailor.com/jobs.rss"
        headers: dict[str, str] = ua_headers()
        if state.etag:
            headers["If-None-Match"] = state.etag
        if state.last_modified:
            headers["If-Modified-Since"] = state.last_modified
        resp = await client.get(url, headers=headers, timeout=20.0)

        if resp.status_code == 304:
            return FetchResult(postings=[], new_state=None, not_modified=True)

        resp.raise_for_status()
        root = ET.fromstring(resp.text)

        out: list[RawPosting] = []
        for item in root.iter("item"):
            link = (item.findtext("link") or "").strip()
            status = (item.findtext("remoteStatus") or "").strip().lower()
            wt = _REMOTE_STATUS_TO_WORKPLACE.get(status)
            out.append(
                RawPosting(
                    source=self.name,
                    external_id=(item.findtext("guid") or "").strip() or link,
                    title=(item.findtext("title") or "").strip(),
                    description=_strip_html(item.findtext("description") or ""),
                    apply_url=link,
                    location=annotate_location(_location_text(item), wt),
                    remote=remote_from_workplace_type(wt),
                    department=(item.findtext(f"{_TT_NS}department") or "").strip() or None,
                    company=None,
                    posted_at=_parse_pubdate(item.findtext("pubDate")),
                    comp_min=None,
                    comp_max=None,
                    raw={"guid": (item.findtext("guid") or "").strip()},
                )
            )

        new_state = ConnectorState(
            etag=resp.headers.get("etag"),
            last_modified=resp.headers.get("last-modified"),
        )
        return FetchResult(postings=out, new_state=new_state, not_modified=False)
