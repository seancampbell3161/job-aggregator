from __future__ import annotations

import html
import re
from datetime import datetime, timezone

import httpx

from src.models import ConnectorState, FetchResult, RawPosting
from src.user_agent import headers as ua_headers

_BASE_URL = "https://remoteok.com/api"


def _strip_html(s: str) -> str:
    s = html.unescape(s)
    s = re.sub(r"<[^>]+>", "", s)
    return re.sub(r"\s+", " ", s).strip()


class RemoteOkConnector:
    name: str = "remoteok"
    tier: str = "slow"

    async def fetch(self, client: httpx.AsyncClient, state: ConnectorState) -> FetchResult:
        resp = await client.get(
            _BASE_URL,
            headers=ua_headers(),
            timeout=30.0,
        )
        resp.raise_for_status()
        data = resp.json()

        out: list[RawPosting] = []
        for j in data:
            # First element is metadata (has a 'legal' key) — skip it
            if "legal" in j:
                continue

            tags: list[str] = j.get("tags") or []
            description = _strip_html(j.get("description", ""))
            if tags:
                description = description + "\nTags: " + ", ".join(tags)

            posted_at = None
            if j.get("epoch"):
                try:
                    posted_at = datetime.fromtimestamp(int(j["epoch"]), tz=timezone.utc)
                except (ValueError, OSError):
                    posted_at = None

            # Treat 0 salary as "not provided"
            salary_min = j.get("salary_min")
            salary_max = j.get("salary_max")
            comp_min = int(salary_min) if salary_min else None
            comp_max = int(salary_max) if salary_max else None

            apply_url = j.get("apply_url") or j.get("url") or ""

            out.append(
                RawPosting(
                    source="remoteok",
                    external_id=str(j["id"]),
                    title=j.get("position") or "",
                    description=description,
                    apply_url=apply_url,
                    location=j.get("location") or None,
                    remote=True,
                    posted_at=posted_at,
                    comp_min=comp_min,
                    comp_max=comp_max,
                    raw=j,
                )
            )
        return FetchResult(postings=out, new_state=None, not_modified=False)
