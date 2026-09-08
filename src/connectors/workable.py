from __future__ import annotations

import html
import re
from datetime import datetime

import httpx

from src.models import ConnectorState, FetchResult, RawPosting
from src.user_agent import headers as ua_headers


def _strip_html(s: str) -> str:
    s = html.unescape(s)
    s = re.sub(r"<[^>]+>", "", s)
    return re.sub(r"\s+", " ", s).strip()


class WorkableConnector:
    name: str
    # Slow tier (15-min cadence), not ats: the boards are tiny and
    # apply.workable.com 429-rate-limits this IP under 60s polling. Tier
    # membership is decided in build_connectors; this attribute is informational.
    tier: str = "slow"

    def __init__(self, slug: str) -> None:
        self.slug = slug
        self.name = f"workable:{slug}"

    async def fetch(self, client: httpx.AsyncClient, state: ConnectorState) -> FetchResult:
        url = f"https://apply.workable.com/api/v3/accounts/{self.slug}/jobs"
        headers: dict[str, str] = ua_headers()
        if state.etag:
            headers["If-None-Match"] = state.etag
        if state.last_modified:
            headers["If-Modified-Since"] = state.last_modified
        resp = await client.post(url, json={}, headers=headers, timeout=20.0)

        if resp.status_code == 304:
            return FetchResult(postings=[], new_state=None, not_modified=True)

        resp.raise_for_status()
        data = resp.json()
        out: list[RawPosting] = []
        for j in data.get("results", []):
            posted_at = None
            if j.get("created_at"):
                try:
                    posted_at = datetime.fromisoformat(j["created_at"].replace("Z", "+00:00"))
                except ValueError:
                    posted_at = None
            loc = j.get("location") or {}
            location_text_parts = [p for p in [loc.get("city"), loc.get("country")] if p]
            workplace = (loc.get("workplace") or "").lower()
            out.append(
                RawPosting(
                    source=self.name,
                    external_id=str(j.get("shortcode") or j.get("id")),
                    title=j.get("title") or "",
                    description=_strip_html(j.get("description", "")),
                    apply_url=j.get("application_url") or j.get("url", ""),
                    location=", ".join(location_text_parts) or None,
                    remote=workplace == "remote" if workplace else None,
                    department=j.get("department"),
                    posted_at=posted_at,
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
