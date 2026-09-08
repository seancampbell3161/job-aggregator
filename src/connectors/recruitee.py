"""Recruitee (Tellent) — public careers-site API, one board per tenant.

GET https://{slug}.recruitee.com/api/offers/ returns {"offers": [...]} with no
authentication. Timestamps look like "2025-12-21 10:19:52 UTC" (not ISO).
Workplace is three booleans (remote/hybrid/on_site); structured city/country
fields are present and preferred over the free-text location string."""

from __future__ import annotations

import html
import re
from datetime import datetime
from typing import Any

import httpx

from src.connectors.workplace import annotate_location, remote_from_workplace_type
from src.models import ConnectorState, FetchResult, RawPosting
from src.user_agent import headers as ua_headers


def _strip_html(s: str) -> str:
    s = html.unescape(s)
    s = re.sub(r"<[^>]+>", "", s)
    return re.sub(r"\s+", " ", s).strip()


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace(" UTC", "+00:00"))
    except ValueError:
        return None


def _workplace_type(offer: dict[str, Any]) -> str | None:
    if offer.get("remote"):
        return "remote"
    if offer.get("hybrid"):
        return "hybrid"
    if offer.get("on_site"):
        return "onsite"
    return None


def _location_text(offer: dict[str, Any]) -> str | None:
    parts = [p for p in [offer.get("city"), offer.get("country")] if p]
    return ", ".join(parts) or offer.get("location") or None


class RecruiteeConnector:
    name: str
    tier: str = "ats"

    def __init__(self, slug: str) -> None:
        self.slug = slug
        self.name = f"recruitee:{slug}"

    async def fetch(self, client: httpx.AsyncClient, state: ConnectorState) -> FetchResult:
        url = f"https://{self.slug}.recruitee.com/api/offers/"
        headers: dict[str, str] = ua_headers()
        if state.etag:
            headers["If-None-Match"] = state.etag
        if state.last_modified:
            headers["If-Modified-Since"] = state.last_modified
        resp = await client.get(url, headers=headers, timeout=20.0)

        if resp.status_code == 304:
            return FetchResult(postings=[], new_state=None, not_modified=True)

        resp.raise_for_status()
        data = resp.json()

        out: list[RawPosting] = []
        for offer in data.get("offers", []):
            wt = _workplace_type(offer)
            description = "\n".join(
                part
                for part in (
                    _strip_html(offer.get("description") or ""),
                    _strip_html(offer.get("requirements") or ""),
                )
                if part
            )
            out.append(
                RawPosting(
                    source=self.name,
                    external_id=str(offer.get("id") or ""),
                    title=offer.get("title") or "",
                    description=description,
                    apply_url=offer.get("careers_url") or offer.get("careers_apply_url") or "",
                    location=annotate_location(_location_text(offer), wt),
                    remote=remote_from_workplace_type(wt),
                    department=offer.get("department"),
                    company=offer.get("company_name"),
                    posted_at=_parse_dt(offer.get("published_at") or offer.get("created_at")),
                    comp_min=None,
                    comp_max=None,
                    raw=offer,
                )
            )

        new_state = ConnectorState(
            etag=resp.headers.get("etag"),
            last_modified=resp.headers.get("last-modified"),
        )
        return FetchResult(postings=out, new_state=new_state, not_modified=False)
