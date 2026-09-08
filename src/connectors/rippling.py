from __future__ import annotations

import httpx

from src.models import ConnectorState, FetchResult, RawPosting
from src.user_agent import headers as ua_headers


class RipplingConnector:
    """Rippling ATS board connector. Uses the public, slug-addressable board
    list API: https://api.rippling.com/platform/api/ats/v1/board/{slug}/jobs

    The list endpoint returns title/location/department/url only — no
    description, comp, or post-date (those live on a per-job detail endpoint we
    deliberately don't fetch; see the spec). Company is derived from the slug by
    the normalizer."""

    name: str
    tier: str = "ats"

    def __init__(self, slug: str) -> None:
        self.slug = slug
        self.name = f"rippling:{slug}"

    async def fetch(self, client: httpx.AsyncClient, state: ConnectorState) -> FetchResult:
        url = f"https://api.rippling.com/platform/api/ats/v1/board/{self.slug}/jobs"
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
        if not isinstance(data, list):
            data = []

        out: list[RawPosting] = []
        for j in data:
            if not isinstance(j, dict):
                continue
            uuid = j.get("uuid")
            if not uuid:
                # No stable id → would produce a colliding job_id ("rippling:slug:").
                # Skip rather than corrupt dedup.
                continue
            work_location = j.get("workLocation") or {}
            department = j.get("department") or {}
            out.append(
                RawPosting(
                    source=self.name,
                    external_id=str(uuid),
                    title=j.get("name") or "",
                    description="",
                    apply_url=j.get("url", ""),
                    location=work_location.get("label"),
                    department=department.get("label"),
                    posted_at=None,
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
