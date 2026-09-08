from __future__ import annotations

import asyncio
import html
import re
from datetime import datetime, timezone
from typing import Any

import httpx
from bs4 import BeautifulSoup

from src.models import ConnectorState, FetchResult, RawPosting
from src.user_agent import headers as ua_headers

_BASE = "https://hacker-news.firebaseio.com/v0"
_THREAD_TITLE_RE = re.compile(r"who\s*is\s*hiring", re.IGNORECASE)
_MAX_COMMENTS = 200


def _strip_html(text: str) -> str:
    soup = BeautifulSoup(html.unescape(text), "html.parser")
    return re.sub(r"\s+", " ", soup.get_text(separator=" ")).strip()


def _first_line(text: str) -> str:
    plain = _strip_html(text)
    # Conventional HN headline: "Company | Role | Location | Type | URL\n<p>body</p>".
    return plain.split(". ")[0][:300]


def _parse_headline(headline: str) -> tuple[str, str | None, bool | None]:
    """Return (title, location, remote)."""
    parts = [p.strip() for p in headline.split("|")]
    if len(parts) >= 3:
        company = parts[0]
        role = parts[1]
        location = parts[2]
        title = f"{company} | {role}"
    elif len(parts) == 2:
        title = " | ".join(parts)
        location = None
    else:
        title = headline
        location = None
    remote = True if location and "remote" in location.lower() else (
        False if location and "onsite" in location.lower() else None
    )
    return title, location, remote


class HnWhoIsHiringConnector:
    name: str = "hn:who_is_hiring"
    tier: str = "slow"

    async def fetch(self, client: httpx.AsyncClient, state: ConnectorState) -> FetchResult:
        user = (await client.get(f"{_BASE}/user/whoishiring.json", headers=ua_headers(), timeout=20.0)).json()
        submitted: list[int] = user.get("submitted") or []

        thread: dict[str, Any] | None = None
        for item_id in submitted[:30]:
            item = (await client.get(f"{_BASE}/item/{item_id}.json", headers=ua_headers(), timeout=20.0)).json()
            if item and item.get("type") == "story" and _THREAD_TITLE_RE.search(item.get("title", "")):
                thread = item
                break
        if thread is None:
            return FetchResult(postings=[], new_state=None, not_modified=False)

        kid_ids: list[int] = (thread.get("kids") or [])[:_MAX_COMMENTS]

        async def _fetch_kid(kid_id: int) -> dict[str, Any] | None:
            r = await client.get(f"{_BASE}/item/{kid_id}.json", headers=ua_headers(), timeout=20.0)
            r.raise_for_status()
            return r.json()

        kids = await asyncio.gather(*(_fetch_kid(k) for k in kid_ids), return_exceptions=True)

        out: list[RawPosting] = []
        for k in kids:
            if not isinstance(k, dict):
                continue
            if k.get("deleted") or k.get("dead"):
                continue
            text = k.get("text") or ""
            if not text.strip():
                continue
            headline = _first_line(text)
            # HN job postings follow "Company | Role | Location | ..." convention.
            # Comments without a `|` are conversation, not job postings — skip them
            # so a stray reply (or a moderator-edited comment) can't slip into the
            # title-filter pipeline.
            if "|" not in headline:
                continue
            title, location, remote = _parse_headline(headline)
            out.append(
                RawPosting(
                    source=self.name,
                    external_id=str(k["id"]),
                    title=title,
                    description=_strip_html(text),
                    apply_url=f"https://news.ycombinator.com/item?id={k['id']}",
                    location=location,
                    remote=remote,
                    posted_at=datetime.fromtimestamp(int(k["time"]), tz=timezone.utc),
                    raw=k,
                )
            )
        return FetchResult(postings=out, new_state=None, not_modified=False)
