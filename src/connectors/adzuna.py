"""Adzuna job-search API connector (slow tier, opt-in).

Hybrid discovery source: emits scoreable RawPostings for results whose
company we don't already poll directly, and emits Sightings (the
conversion-chain input) so ATS-hosted companies get their board
auto-added as a permanent direct poll. Descriptions are Adzuna's snippet
only."""

from __future__ import annotations

import asyncio
import html
import logging
import re
from datetime import datetime, timezone

import httpx

from src.connectors.base import _board_active_pair
from src.models import ConnectorState, FetchResult, RawPosting
from src.sightings import Sighting, classify_sighting
from src.user_agent import headers as ua_headers

log = logging.getLogger(__name__)

_API_BASE = "https://api.adzuna.com/v1/api/jobs"
_SEEN_CAP = 3000            # ≈ several weeks of new-result volume
_RESOLVE_CONCURRENCY = 5    # polite bound on parallel redirect follows
_RESOLVE_TIMEOUT = 15.0


def _strip_html(s: str) -> str:
    s = html.unescape(s)
    s = re.sub(r"<[^>]+>", "", s)
    return re.sub(r"\s+", " ", s).strip()


def _parse_created(created: str | None) -> datetime | None:
    if not created:
        return None
    try:
        return datetime.fromisoformat(created.replace("Z", "+00:00"))
    except ValueError:
        return None


class AdzunaConnector:
    name: str = "adzuna"
    tier: str = "slow"

    def __init__(
        self, *, app_id: str, app_key: str, countries: list[str], queries: list[str],
        max_days_old: int, results_per_page: int, daily_call_budget: int,
        active_set: set[tuple[str, str]] | None = None,
        sightings: list[Sighting] | None = None,
    ) -> None:
        self._app_id = app_id
        self._app_key = app_key
        self._countries = countries
        self._queries = queries
        self._max_days_old = max_days_old
        self._per_page = results_per_page
        self._budget = daily_call_budget
        self._active_set = active_set or set()
        self._sightings = sightings

    async def fetch(self, client: httpx.AsyncClient, state: ConnectorState) -> FetchResult:
        payload = dict(state.payload or {})
        today = datetime.now(timezone.utc).date().isoformat()
        calls = payload.get("budget_calls", 0) if payload.get("budget_date") == today else 0
        seen_ids: list[str] = list(payload.get("seen_ids", []))
        seen_set = set(seen_ids)

        results: list[dict] = []
        first_call = True
        stop = False
        for country in self._countries:
            if stop:
                break
            for query in self._queries:
                if calls >= self._budget:
                    log.info(
                        "adzuna_daily_budget_exhausted",
                        extra={"budget": self._budget, "date": today},
                    )
                    stop = True
                    break
                try:
                    resp = await client.get(
                        f"{_API_BASE}/{country}/search/1",
                        params={
                            "app_id": self._app_id,
                            "app_key": self._app_key,
                            "what": query,
                            "results_per_page": self._per_page,
                            "max_days_old": self._max_days_old,
                            "sort_by": "date",
                        },
                        headers=ua_headers(),
                        timeout=30.0,
                    )
                    calls += 1
                    resp.raise_for_status()
                    data = resp.json()
                except (httpx.HTTPError, ValueError):
                    # Known leak: calls made before a first-call raise are never
                    # persisted (no new_state on the raise path) — acceptable,
                    # failing calls rarely consume metered quota.
                    if first_call:
                        raise  # clean first failure → poll_health classifies 401/429/5xx
                    log.exception(
                        "adzuna_query_failed", extra={"country": country, "query": query}
                    )
                    stop = True
                    break
                first_call = False
                results.extend(data.get("results", []))

        # Never-seen results only (cross-query dedup within the batch too).
        new_results: list[dict] = []
        for r in results:
            rid = str(r.get("id", ""))
            if not rid or rid in seen_set:
                continue
            seen_set.add(rid)
            seen_ids.append(rid)
            new_results.append(r)

        sem = asyncio.Semaphore(_RESOLVE_CONCURRENCY)
        finals = await asyncio.gather(
            *(self._resolve(client, r.get("redirect_url", ""), sem) for r in new_results)
        )

        out: list[RawPosting] = []
        for r, final_url in zip(new_results, finals):
            posting = self._build_posting(r, final_url)
            if posting is not None:
                out.append(posting)

        if len(seen_ids) > _SEEN_CAP:
            seen_ids = seen_ids[-_SEEN_CAP:]
        new_state = ConnectorState(
            payload={"budget_date": today, "budget_calls": calls, "seen_ids": seen_ids}
        )
        return FetchResult(postings=out, new_state=new_state, not_modified=False)

    async def _resolve(
        self, client: httpx.AsyncClient, url: str, sem: asyncio.Semaphore
    ) -> str | None:
        """Final URL behind Adzuna's redirect, else None. HEAD first; a GET
        (streamed, body discarded) covers servers that drop HEAD connections.
        Any terminal status is fine — we only want the post-redirect URL."""
        if not url:
            return None
        async with sem:
            try:
                resp = await client.head(url, headers=ua_headers(), follow_redirects=True, timeout=_RESOLVE_TIMEOUT)
                return str(resp.url)
            except httpx.HTTPError:
                try:
                    async with client.stream(
                        "GET", url, headers=ua_headers(), follow_redirects=True, timeout=_RESOLVE_TIMEOUT
                    ) as resp:
                        return str(resp.url)
                except httpx.HTTPError:
                    return None

    def _build_posting(self, r: dict, final_url: str | None) -> RawPosting | None:
        """RawPosting for one API result, or None when the destination is a
        board we already poll directly (the direct poll owns it). Emits a
        Sighting for every resolved, non-suppressed result — the drain
        classifies ATS-hosted ones into discovery candidates."""
        rid = str(r.get("id", ""))
        company = (r.get("company") or {}).get("display_name") or ""
        if final_url:
            s = Sighting(
                ats_family=None, slug=None, company=company,
                apply_url=final_url, origin="adzuna",
            )
            classified = classify_sighting(s)
            if classified is not None:
                pair = _board_active_pair(classified[0], dict(classified[1]))
                if pair is not None and pair in self._active_set:
                    return None
            if self._sightings is not None:
                self._sightings.append(s)

        comp_min = comp_max = None
        if str(r.get("salary_is_predicted", "")) != "1":
            if r.get("salary_min"):
                comp_min = int(r["salary_min"])
            if r.get("salary_max"):
                comp_max = int(r["salary_max"])

        return RawPosting(
            source="adzuna",
            external_id=rid,
            title=r.get("title") or "",
            description=_strip_html(r.get("description", "")),
            apply_url=final_url or r.get("redirect_url", ""),
            location=(r.get("location") or {}).get("display_name"),
            company=company or None,
            posted_at=_parse_created(r.get("created")),
            comp_min=comp_min,
            comp_max=comp_max,
            employment_type=r.get("contract_time") or r.get("contract_type"),
            raw={},
        )
