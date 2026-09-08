from __future__ import annotations

from datetime import datetime, timezone

import httpx

from src.connectors.workplace import annotate_location, remote_from_workplace_type
from src.models import ConnectorState, FetchResult, RawPosting
from src.user_agent import headers as ua_headers


class LeverConnector:
    name: str
    tier: str = "ats"

    def __init__(self, slug: str) -> None:
        self.slug = slug
        self.name = f"lever:{slug}"

    async def fetch(self, client: httpx.AsyncClient, state: ConnectorState) -> FetchResult:
        url = f"https://api.lever.co/v0/postings/{self.slug}"
        headers: dict[str, str] = ua_headers()
        if state.etag:
            headers["If-None-Match"] = state.etag
        if state.last_modified:
            headers["If-Modified-Since"] = state.last_modified
        resp = await client.get(url, params={"mode": "json"}, headers=headers, timeout=20.0)

        if resp.status_code == 304:
            return FetchResult(postings=[], new_state=None, not_modified=True)

        resp.raise_for_status()
        data = resp.json()
        out: list[RawPosting] = []
        for j in data:
            posted_at = None
            if j.get("createdAt"):
                posted_at = datetime.fromtimestamp(int(j["createdAt"]) / 1000, tz=timezone.utc)
            cats = j.get("categories") or {}
            sal = j.get("salaryRange") or {}
            out.append(
                RawPosting(
                    source=self.name,
                    external_id=str(j["id"]),
                    title=j.get("text") or "",
                    description=j.get("descriptionPlain") or j.get("description") or "",
                    apply_url=j.get("hostedUrl", ""),
                    # Lever exposes workplaceType (remote/hybrid/onsite); use it
                    # for the remote flag and fold hybrid/onsite into the text.
                    location=annotate_location(cats.get("location"), j.get("workplaceType")),
                    remote=remote_from_workplace_type(j.get("workplaceType")),
                    department=cats.get("team"),
                    posted_at=posted_at,
                    comp_min=sal.get("min"),
                    comp_max=sal.get("max"),
                    # Lever's categories.commitment: "Full-time" | "Contract" | ...
                    employment_type=cats.get("commitment"),
                    raw=j,
                )
            )
        new_state = ConnectorState(
            etag=resp.headers.get("etag"),
            last_modified=resp.headers.get("last-modified"),
        )
        return FetchResult(postings=out, new_state=new_state, not_modified=False)
