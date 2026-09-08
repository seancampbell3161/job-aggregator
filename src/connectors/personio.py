"""Personio — public per-tenant XML job feed (DACH SMB leader).

GET https://{slug}.jobs.personio.de/xml (some tenants live on .com instead —
we try .de then .com; only a direct 200/304 counts, because unknown slugs
307-redirect to the personio.com marketing site). The feed is fetched in the
tenant's default language: the ?language=en variant returns EMPTY descriptions
for tenants that only maintain German content (verified live), and tech stack
keywords survive German prose anyway. Root element is <workzag-jobs>;
positions may lack <createdAt> (posted_at=None rides filter_age's UNKNOWN
path, the Rippling precedent). <office> is free text and may be German — the
LLM profile is the backstop for strings the geo table can't resolve."""

from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from datetime import datetime

import httpx

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
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _description(pos: ET.Element) -> str:
    parts: list[str] = []
    for jd in pos.findall("jobDescriptions/jobDescription"):
        name = (jd.findtext("name") or "").strip()
        value = _strip_html(jd.findtext("value") or "")
        if value:
            parts.append(f"{name}: {value}" if name else value)
    return "\n".join(parts)


class PersonioConnector:
    name: str
    tier: str = "ats"

    def __init__(self, slug: str) -> None:
        self.slug = slug
        self.name = f"personio:{slug}"

    async def _get_feed(self, client: httpx.AsyncClient, headers: dict[str, str]) -> tuple[httpx.Response, str]:
        last: httpx.Response | None = None
        for host in (f"{self.slug}.jobs.personio.de", f"{self.slug}.jobs.personio.com"):
            resp = await client.get(f"https://{host}/xml", headers=headers, timeout=20.0)
            if resp.status_code in (200, 304):
                return resp, host
            last = resp
        assert last is not None
        # raise_for_status raises on ANY non-2xx (incl. the 307 marketing
        # redirect for unknown slugs), so this is the terminal path for both
        # dead (4xx/5xx) and redirected (not-a-tenant) hosts. NOTE:
        # poll-health classifies a 307 as transient, not dead — a
        # deprovisioned tenant re-polls until discovery revalidation
        # quarantines it (tracked fast-follow).
        last.raise_for_status()

    async def fetch(self, client: httpx.AsyncClient, state: ConnectorState) -> FetchResult:
        headers: dict[str, str] = ua_headers()
        if state.etag:
            headers["If-None-Match"] = state.etag
        if state.last_modified:
            headers["If-Modified-Since"] = state.last_modified
        resp, host = await self._get_feed(client, headers)

        if resp.status_code == 304:
            return FetchResult(postings=[], new_state=None, not_modified=True)

        root = ET.fromstring(resp.text)
        out: list[RawPosting] = []
        for pos in root.iter("position"):
            pos_id = (pos.findtext("id") or "").strip()
            out.append(
                RawPosting(
                    source=self.name,
                    external_id=pos_id,
                    title=(pos.findtext("name") or "").strip(),
                    description=_description(pos),
                    apply_url=f"https://{host}/job/{pos_id}",
                    location=(pos.findtext("office") or "").strip() or None,
                    remote=None,
                    department=(pos.findtext("department") or "").strip() or None,
                    company=(pos.findtext("subcompany") or "").strip() or None,
                    posted_at=_parse_dt(pos.findtext("createdAt")),
                    comp_min=None,
                    comp_max=None,
                    raw={"id": pos_id},
                )
            )

        new_state = ConnectorState(
            etag=resp.headers.get("etag"),
            last_modified=resp.headers.get("last-modified"),
        )
        return FetchResult(postings=out, new_state=new_state, not_modified=False)
