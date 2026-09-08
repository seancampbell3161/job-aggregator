from __future__ import annotations

import html
import re
from datetime import datetime

import httpx

from src.models import ConnectorState, FetchResult, RawPosting
from src.user_agent import headers as ua_headers

_BASE_URL = "https://remotive.com/api/remote-jobs"
_COMP_RE = re.compile(r"\$([\d,]+)\s*[-–to]+\s*\$([\d,]+)", re.IGNORECASE)


def _strip_html(s: str) -> str:
    s = html.unescape(s)
    s = re.sub(r"<[^>]+>", "", s)
    return re.sub(r"\s+", " ", s).strip()


def _parse_comp(salary: str) -> tuple[int | None, int | None]:
    """Parse salary strings like '$150,000 - $200,000' or '$150k - $200k'."""
    if not salary:
        return None, None
    # Normalise 'k' suffix before matching
    normalised = re.sub(r"\$([\d,]+)k", lambda m: f"${int(m.group(1).replace(',', '')) * 1000}", salary, flags=re.IGNORECASE)
    match = _COMP_RE.search(normalised)
    if match:
        lo = int(match.group(1).replace(",", ""))
        hi = int(match.group(2).replace(",", ""))
        return lo, hi
    return None, None


class RemotiveConnector:
    name: str = "remotive"
    tier: str = "slow"

    async def fetch(self, client: httpx.AsyncClient, state: ConnectorState) -> FetchResult:
        resp = await client.get(
            _BASE_URL,
            params={"category": "software-dev", "limit": 100},
            headers=ua_headers(),
            timeout=30.0,
        )
        resp.raise_for_status()
        data = resp.json()

        out: list[RawPosting] = []
        for j in data.get("jobs", []):
            posted_at = None
            if j.get("publication_date"):
                try:
                    posted_at = datetime.fromisoformat(j["publication_date"])
                except ValueError:
                    posted_at = None

            comp_min, comp_max = _parse_comp(j.get("salary") or "")

            tags: list[str] = j.get("tags") or []
            description = _strip_html(j.get("description", ""))
            if tags:
                description = description + "\nTags: " + ", ".join(tags)

            out.append(
                RawPosting(
                    source="remotive",
                    external_id=str(j["id"]),
                    title=j.get("title") or "",
                    description=description,
                    apply_url=j.get("url", ""),
                    location=j.get("candidate_required_location") or None,
                    remote=True,
                    posted_at=posted_at,
                    comp_min=comp_min,
                    comp_max=comp_max,
                    raw=j,
                )
            )
        return FetchResult(postings=out, new_state=None, not_modified=False)
