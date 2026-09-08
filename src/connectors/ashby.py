from __future__ import annotations

import re
from datetime import datetime
from typing import Any

import httpx

from src.connectors.workplace import annotate_location, remote_from_workplace_type
from src.models import ConnectorState, FetchResult, RawPosting
from src.user_agent import headers as ua_headers

# Matches "$150K – $200K", "$150,000 - $200,000", "USD 150K - 200K", etc.
_COMP_RE = re.compile(
    r"\$?\s*([\d,]+)\s*[Kk]?\s*[-–—to]+\s*\$?\s*([\d,]+)\s*[Kk]?",
    re.IGNORECASE,
)


def _parse_comp_summary(s: str | None) -> tuple[int | None, int | None]:
    if not s:
        return None, None
    m = _COMP_RE.search(s)
    if not m:
        return None, None

    def _to_int(raw: str, has_k: bool) -> int:
        n = int(raw.replace(",", ""))
        return n * 1000 if has_k else n

    has_k = "k" in s.lower()
    return _to_int(m.group(1), has_k), _to_int(m.group(2), has_k)


def _comp(j: dict[str, Any]) -> tuple[int | None, int | None]:
    comp = j.get("compensation") or {}
    return _parse_comp_summary(comp.get("compensationTierSummary"))


class AshbyConnector:
    name: str
    tier: str = "ats"

    def __init__(self, slug: str) -> None:
        self.slug = slug
        self.name = f"ashby:{slug}"

    async def fetch(self, client: httpx.AsyncClient, state: ConnectorState) -> FetchResult:
        url = f"https://api.ashbyhq.com/posting-api/job-board/{self.slug}"
        headers: dict[str, str] = ua_headers()
        if state.etag:
            headers["If-None-Match"] = state.etag
        if state.last_modified:
            headers["If-Modified-Since"] = state.last_modified
        resp = await client.get(
            url,
            params={"includeCompensation": "true"},
            headers=headers,
            timeout=20.0,
        )

        if resp.status_code == 304:
            return FetchResult(postings=[], new_state=None, not_modified=True)

        resp.raise_for_status()
        data = resp.json()
        out: list[RawPosting] = []
        for j in data.get("jobs", []):
            posted_at = None
            if j.get("publishedAt"):
                try:
                    posted_at = datetime.fromisoformat(j["publishedAt"].replace("Z", "+00:00"))
                except ValueError:
                    posted_at = None
            comp_min, comp_max = _comp(j)
            out.append(
                RawPosting(
                    source=self.name,
                    external_id=str(j["id"]),
                    title=j.get("title") or "",
                    description=j.get("descriptionPlain") or j.get("descriptionHtml") or "",
                    apply_url=j.get("jobUrl", ""),
                    # Honor the explicit workplaceType (Remote/Hybrid/OnSite);
                    # isRemote is routinely left true on hybrid roles. Fold the
                    # type into the location text so hybrid != onsite downstream.
                    location=annotate_location(j.get("location"), j.get("workplaceType")),
                    remote=remote_from_workplace_type(
                        j.get("workplaceType"), fallback=j.get("isRemote")
                    ),
                    department=j.get("department"),
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
