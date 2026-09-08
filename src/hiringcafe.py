"""Hiring.cafe HTTP client + schema-flexible response parser.

DISABLED 2026-09-07 — terminally blocked; see the status comment above ``_BASE``.
Every entry point this module needs is closed. The code is kept intact for the
day an API key makes it usable again, not because it is expected to work.

Hiring.cafe is a meta-aggregator that crawls dozens of ATSs (Greenhouse, Lever,
Ashby, Workable, SmartRecruiters, iCIMS, Workday, Recruitee, Personio, etc.).
Their site is server-side-rendered with Next.js; the search results live in
``pageProps.ssrHits`` of an Algolia-shaped payload.

The endpoint URL embeds a build hash (e.g. ``/_next/data/<hash>/index.json``)
which rotates on deploy. We construct the URL freshly each request by reading
the build hash from the ``/jobs`` page HTML, then issuing the JSON request."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx
from src.user_agent import headers as ua_headers

log = logging.getLogger(__name__)

_BUILD_ID_RE = re.compile(r'"buildId":"([^"]+)"')

DEFAULT_QUERY = "software engineer"

# The SSR data endpoint ignores a bare ?q= param (verified live 2026-07-11 —
# identical results for any q, including nonsense). Filtering happens through a
# JSON `searchState` param whose shape comes from the site's own search-state
# module (searchQuery / dateFetchedPastNDays / sortBy / locations / ...).
# sortBy=date + a fetched-within window keeps the single SSR page (~100-150
# hits) full of fresh, relevant postings instead of the site's unfiltered
# default list. The window is 2x our max_age_days for headroom — the
# downstream age gate still governs actual freshness. Caveat: dateFetched is
# hiring.cafe's crawl time, not the posting date; a board they crawl slower
# than every 4 days could have fresh postings age out of this window unseen.
_FETCHED_PAST_N_DAYS = 4


def _location_state(location: str) -> dict[str, Any]:
    """Build the verified-minimal `locations` entry (live-tested 2026-07-11).

    Countries match ONLY on address_components short_name (uppercase ISO-2);
    long_name must be present but its value is ignored, so the code doubles
    as the display name. Continents match on formatted_address (canonical
    spelling) with empty address_components. Config validation guarantees
    the input is one of those two forms."""
    if len(location) == 2:  # ISO-2 country code
        return {
            "types": ["country"],
            "formatted_address": location,
            "address_components": [
                {"long_name": location, "short_name": location, "types": ["country"]}
            ],
            "options": {},
        }
    return {
        "types": ["continent"],
        "formatted_address": location,
        "address_components": [],
        "options": {},
    }


def _search_state(query: str, location: str | None = None) -> str:
    state: dict[str, Any] = {
        "searchQuery": query,
        "dateFetchedPastNDays": _FETCHED_PAST_N_DAYS,
        "sortBy": "date",
    }
    # No `locations` and no `defaultToUserLocation` when unscoped: the state
    # stays byte-identical to the pre-geo-scoping version, and the server
    # keeps geo-defaulting by egress IP for unpinned deployments.
    if location:
        state["locations"] = [_location_state(location)]
    return json.dumps(state)


# hiring.cafe's `source` labels don't always match our connector family names
# (observed live 2026-07-11). Unmapped labels pass through lowercased; the
# apply-URL structural parsers remain the primary classifier for sightings.
# NOTE: normalizing changes the `hiringcafe:{family}:{slug}` source branding,
# so postings from these three families notified shortly before this change
# deployed get a new job_id and may re-alert ONCE (bounded by max_age_days,
# self-healing after the first cycle) — accepted over a seen-store migration.
_ATS_FAMILY_ALIASES = {
    "grnhse": "greenhouse",
    "icims2": "icims",
    "taleo_careersection": "taleo",
}


@dataclass(frozen=True)
class HiringCafeJob:
    """A single posting parsed from a Hiring.cafe response."""

    title: str
    company: str
    ats_family: str | None      # "greenhouse" | "lever" | "ashby" | "workable" | "smartrecruiters" | other | None
    ats_slug: str | None
    external_id: str | None     # Hiring.cafe's id; used as the unique key
    apply_url: str
    posted_at: datetime | None
    description: str
    location: str | None
    remote: bool | None
    employment_type: str | None = None  # v5_processed_job_data.commitment, e.g. "Contract"


# STATUS 2026-09-07: terminally blocked. Disabled in config; both entry points
# below are closed, for two independent reasons:
#
#   * /jobs — and "/" and /api/search-jobs — answer 403 with
#     `cf-mitigated: challenge`. The Cloudflare managed challenge that hit the
#     homepage in July has spread to every page this module touches.
#   * the search request itself is now robots-disallowed: robots.txt gained
#     `Disallow: /*?searchState=*`, which matches
#     /_next/data/<buildId>/index.json?searchState={...}. /_next/ is covered by
#     none of their Allow rules.
#
# History: hiring.cafe moved to hiringcafe.com on 2026-07-14 and challenged the
# homepage, where the buildId was scraped at the time, 403ing every poll for 13
# days. The fix read the buildId from /jobs instead — then unchallenged and
# explicitly robots-allowed, a different door rather than a way through the
# locked one. That door is now shut too, which is precisely the case the old
# version of this comment pre-committed to: do NOT solve or bypass the
# challenge, and do not start now — no UA rotation, no solver, no routing it
# through the headless (Playwright) tier. The connector fails and stays failed.
#
# A WAF rule can be over-broad by accident; a robots.txt line is authored on
# purpose, so the second signal is the binding one. The reversible half is not
# the constraint — even if the challenge is lifted, robots still disallows the
# searchState query. The way back is an API key: /api/search-jobs answered 401
# ("authenticate") before it too fell behind the challenge, so they evidently
# prefer identified programmatic users. Prefer that over this path if one ever
# becomes available.
_BASE = "https://hiringcafe.com"
_BUILD_ID_SOURCE = f"{_BASE}/jobs"


class HiringCafeClient:
    """Schema-flexible Hiring.cafe client.

    The Next.js SSR data URL changes whenever they redeploy (the build hash is
    embedded in the path), so we resolve it fresh on each call by scraping
    ``/jobs``. That page was unchallenged and robots-allowed when this was
    written; as of 2026-09-07 it answers 403 like the rest of the site, so every
    call here now raises at ``raise_for_status``. That is the intended
    behavior — see the status comment above ``_BASE``."""

    async def _resolve_build_id(self, client: httpx.AsyncClient) -> str:
        """Scrape the current Next.js buildId from the /jobs page HTML."""
        resp = await client.get(
            _BUILD_ID_SOURCE,
            headers=ua_headers(),
            timeout=30.0,
            follow_redirects=True,
        )
        resp.raise_for_status()
        m = _BUILD_ID_RE.search(resp.text)
        if not m:
            raise RuntimeError(
                f"hiringcafe: could not locate buildId in {_BUILD_ID_SOURCE} HTML"
            )
        return m.group(1)

    async def search(
        self,
        client: httpx.AsyncClient,
        *,
        query: str = DEFAULT_QUERY,
        location: str | None = None,
    ) -> dict[str, Any]:
        """Fetch the SSR page-data JSON for a search.

        Returns the parsed JSON body. Use ``parse_jobs`` to extract postings."""
        build_id = await self._resolve_build_id(client)
        url = f"{_BASE}/_next/data/{build_id}/index.json"
        resp = await client.get(
            url,
            params={"searchState": _search_state(query, location)},
            headers=ua_headers(Accept="application/json"),
            timeout=30.0,
        )
        resp.raise_for_status()
        return resp.json()


async def fetch_jobs_multi(
    cafe_client: "HiringCafeClient",
    client: httpx.AsyncClient,
    searches: Sequence[tuple[str, str | None]],
) -> list[HiringCafeJob]:
    """Run one search per (query, location) pair and concatenate parsed jobs
    in order, deduped by hiring.cafe posting id (the same posting can match
    several searches — e.g. overlapping DE and Europe scopes). Each search
    re-resolves the Next.js buildId (2 HTTP requests per search) — fine at
    one slow-tier cycle cadence."""
    seen: set[str] = set()
    out: list[HiringCafeJob] = []
    for q, loc in searches:
        payload = await cafe_client.search(client, query=q, location=loc)
        for job in parse_jobs(payload):
            key = job.external_id or f"{job.ats_family}:{job.ats_slug}:{job.title}"
            if key in seen:
                continue
            seen.add(key)
            out.append(job)
    return out


# ---------- parsing ----------

def _str_or_none(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _parse_dt(s: Any) -> datetime | None:
    if not isinstance(s, str) or not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _coerce_hits(payload: Any) -> list[dict[str, Any]]:
    """Walk known top-level keys for the postings list.

    Hiring.cafe's actual shape: payload['pageProps']['ssrHits'] (Next.js SSR).
    We also tolerate a flat shape (``hits`` / ``results`` / ``data`` at the top
    level) for forward-compat in case they ever expose a real REST API."""
    if not isinstance(payload, dict):
        return []
    pp = payload.get("pageProps")
    if isinstance(pp, dict):
        hits = pp.get("ssrHits") or pp.get("hits")
        if isinstance(hits, list):
            return hits
    for key in ("ssrHits", "hits", "results", "jobs", "data", "items", "content"):
        v = payload.get(key)
        if isinstance(v, list):
            return v
    return []


def _company_name(hit: dict[str, Any]) -> str:
    enriched = hit.get("enriched_company_data") or {}
    if isinstance(enriched, dict):
        for key in ("name", "title", "company_name", "display_name"):
            v = enriched.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
    # fallback: check v5_processed_job_data.company_name
    v5 = hit.get("v5_processed_job_data") or {}
    if isinstance(v5, dict):
        v = v5.get("company_name")
        if isinstance(v, str) and v.strip():
            return v.strip()
    # last resort: pretty-print the board_token
    board = hit.get("board_token")
    if isinstance(board, str) and board:
        return board.replace("-", " ").replace("_", " ").title()
    return ""


def _posted_at(hit: dict[str, Any]) -> datetime | None:
    v5 = hit.get("v5_processed_job_data") or {}
    if isinstance(v5, dict):
        # estimated_publish_date is the primary field in the fixture
        for key in (
            "estimated_publish_date",
            "posted_date",
            "posted_at",
            "date_posted",
            "first_seen",
            "publication_date",
        ):
            dt = _parse_dt(v5.get(key))
            if dt:
                return dt
        # millis fallback
        millis = v5.get("estimated_publish_date_millis")
        if isinstance(millis, (int, float)) and millis:
            try:
                from datetime import timezone
                return datetime.fromtimestamp(millis / 1000, tz=timezone.utc)
            except (OSError, OverflowError, ValueError):
                pass
    enriched = hit.get("enriched_company_data") or {}
    if isinstance(enriched, dict):
        for key in ("posted_date", "posted_at"):
            dt = _parse_dt(enriched.get(key))
            if dt:
                return dt
    return _parse_dt(hit.get("posted_at"))


def _location(hit: dict[str, Any]) -> str | None:
    v5 = hit.get("v5_processed_job_data") or {}
    if isinstance(v5, dict):
        # formatted_workplace_location is present in the fixture
        for key in (
            "formatted_workplace_location",
            "location",
            "location_string",
            "formatted_location",
        ):
            loc_str = v5.get(key)
            if isinstance(loc_str, str) and loc_str.strip():
                return loc_str.strip()
        # fallback: first city or state
        for list_key in ("workplace_cities", "workplace_states"):
            locs = v5.get(list_key)
            if isinstance(locs, list) and locs:
                first = locs[0]
                if isinstance(first, str) and first.strip():
                    return first.strip()
    return None


def _remote(hit: dict[str, Any]) -> bool | None:
    v5 = hit.get("v5_processed_job_data") or {}
    if isinstance(v5, dict):
        # workplace_type is a string: "Remote" | "Hybrid" | "Onsite"
        wt = v5.get("workplace_type")
        if isinstance(wt, str):
            return wt.strip().lower() == "remote"
        # boolean fields (forward-compat)
        for key in ("is_remote", "remote", "is_fully_remote"):
            v = v5.get(key)
            if isinstance(v, bool):
                return v
    return None


def _commitment(hit: dict[str, Any]) -> str | None:
    """Raw employment-type string from v5_processed_job_data.commitment, which
    Hiring.cafe emits as a list like ["Full Time"] / ["Contract"]. Returns the
    first entry; None when absent (normalize_employment_type handles the rest)."""
    v5 = hit.get("v5_processed_job_data") or {}
    if isinstance(v5, dict):
        commit = v5.get("commitment")
        if isinstance(commit, list):
            for c in commit:
                if isinstance(c, str) and c.strip():
                    return c
        elif isinstance(commit, str) and commit.strip():
            return commit
    return None


def parse_jobs(payload: Any) -> list[HiringCafeJob]:
    """Defensive parser. Returns [] on unrecognized shapes."""
    raw_hits = _coerce_hits(payload)
    out: list[HiringCafeJob] = []
    for h in raw_hits:
        if not isinstance(h, dict):
            continue
        try:
            job_info = h.get("job_information") or {}
            if not isinstance(job_info, dict):
                job_info = {}

            title = (
                _str_or_none(job_info.get("title"))
                or _str_or_none(job_info.get("job_title_raw"))
                or ""
            )
            apply_url = _str_or_none(h.get("apply_url")) or ""
            if not title or not apply_url:
                continue

            ats_family_raw = _str_or_none(h.get("source"))
            ats_family = ats_family_raw.lower() if ats_family_raw else None
            if ats_family:
                ats_family = _ATS_FAMILY_ALIASES.get(ats_family, ats_family)
            ats_slug = _str_or_none(h.get("board_token"))
            external_id = _str_or_none(h.get("id")) or _str_or_none(h.get("objectID"))
            description = _str_or_none(job_info.get("description")) or ""

            out.append(
                HiringCafeJob(
                    title=title,
                    company=_company_name(h),
                    ats_family=ats_family,
                    ats_slug=ats_slug,
                    external_id=external_id,
                    apply_url=apply_url,
                    posted_at=_posted_at(h),
                    description=description,
                    location=_location(h),
                    remote=_remote(h),
                    employment_type=_commitment(h),
                )
            )
        except Exception:  # noqa: BLE001 — defensive: one bad entry must not crash a batch
            log.exception("hiringcafe_parse_entry_failed")
            continue
    return out
