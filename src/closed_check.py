"""Posting-closed detection for active board cards.

One request per card per day: family-specific detail probes where the ATS has
a reliable per-posting endpoint, else a hard-404-only apply_url check.
Verdicts: "alive" (posting confirmed live), "miss" (posting verifiably gone
— hard 404/410 or an empty detail payload), "unknown" (network error, 5xx,
or unusable card — no strike either way: a flaky endpoint can neither close
nor revive a card)."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx
from src.user_agent import headers as ua_headers

log = logging.getLogger(__name__)

def _headers() -> dict[str, str]:
    return ua_headers(Accept="application/json")
_TIMEOUT = 15.0
_GONE = (404, 410)


def _external_id(card: dict) -> str:
    return (card.get("job_id") or "").rsplit(":", 1)[-1]


async def _get(client: httpx.AsyncClient, url: str) -> httpx.Response:
    return await client.get(url, headers=_headers(), timeout=_TIMEOUT)


async def _check_workday(client: httpx.AsyncClient, card: dict) -> str:
    # Derive the CXS detail URL from apply_url, exactly like WorkdayConnector.enrich:
    # apply_url = https://{tenant}.{region}.myworkdayjobs.com/{site}{externalPath}
    # detail    = {base}/wday/cxs/{tenant}/{site}{externalPath}
    parsed = urlparse(card.get("apply_url") or "")
    host = parsed.hostname or ""
    parts = card.get("source", "").split(":")  # workday:{tenant}:{site}
    if not host.endswith(".myworkdayjobs.com") or len(parts) != 3 or not parsed.path:
        return "unknown"
    tenant, site = parts[1], parts[2]
    # Strip the leading "/{site}" so the CXS detail path isn't doubled. Tolerate
    # legacy rows stored before the site prefix was added (path already bare).
    site_prefix = f"/{site}"
    external_path = (
        parsed.path[len(site_prefix):]
        if parsed.path.startswith(site_prefix + "/")
        else parsed.path
    )
    detail = f"https://{host}/wday/cxs/{tenant}/{site}{external_path}"
    resp = await _get(client, detail)
    if resp.status_code in _GONE:
        return "miss"
    if resp.status_code != 200:
        return "unknown"
    info = (resp.json() or {}).get("jobPostingInfo") or {}
    return "alive" if info else "miss"


async def _check_oraclecloud(client: httpx.AsyncClient, card: dict) -> str:
    parsed = urlparse(card.get("apply_url") or "")
    host = parsed.hostname or ""
    parts = card.get("source", "").split(":")  # oraclecloud:{tenant}:{site}
    if not host.endswith(".oraclecloud.com") or len(parts) != 3:
        return "unknown"
    site = parts[2]
    rid = _external_id(card)
    url = (
        f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
        f'?onlyData=true&expand=all&finder=ById;siteNumber={site},Id="{rid}"'
    )
    resp = await _get(client, url)
    if resp.status_code in _GONE:
        return "miss"
    if resp.status_code != 200:
        return "unknown"
    items = (resp.json() or {}).get("items") or []
    return "alive" if items else "miss"


async def _check_greenhouse(client: httpx.AsyncClient, card: dict) -> str:
    slug = card.get("source", "").split(":", 1)[-1]
    resp = await _get(client, f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{_external_id(card)}")
    if resp.status_code in _GONE:
        return "miss"
    return "alive" if resp.status_code == 200 else "unknown"


async def _check_lever(client: httpx.AsyncClient, card: dict) -> str:
    slug = card.get("source", "").split(":", 1)[-1]
    resp = await _get(client, f"https://api.lever.co/v0/postings/{slug}/{_external_id(card)}")
    if resp.status_code in _GONE:
        return "miss"
    return "alive" if resp.status_code == 200 else "unknown"


async def _check_apply_url(client: httpx.AsyncClient, card: dict) -> str:
    """Fallback: ONLY a hard 404/410 counts as a miss. Soft-404 pages and JS
    shells return 200 and stay alive — precision over recall."""
    url = card.get("apply_url") or ""
    if not url.startswith(("https://", "http://")):
        return "unknown"
    resp = await _get(client, url)
    if resp.status_code in _GONE:
        return "miss"
    return "alive" if resp.status_code < 500 else "unknown"


_CHECKERS = {
    "workday": _check_workday,
    "oraclecloud": _check_oraclecloud,
    "greenhouse": _check_greenhouse,
    "lever": _check_lever,
}


async def check_posting(client: httpx.AsyncClient, card: dict) -> str:
    """Classify one card's posting as alive/miss/unknown. Never raises."""
    try:
        family = (card.get("source") or "").split(":", 1)[0]
        checker = _CHECKERS.get(family, _check_apply_url)
        return await checker(client, card)
    except Exception as exc:  # noqa: BLE001 — an error can neither close nor revive a card
        log.warning(
            "closed_check_failed",
            extra={"job_id": card.get("job_id"), "error": str(exc)},
        )
        return "unknown"


_ACTIVE_STATUSES = ("interested", "applied", "interviewing", "offer")


async def run_closed_sweep(
    store, *, client_factory=None, concurrency: int = 4, now_iso: str | None = None,
) -> dict:
    """Check every active-column card once and drive the two-strike state
    machine. Fail-soft per card; returns a tally for the log line."""
    tally = {"checked": 0, "misses": 0, "flagged": 0, "reset": 0, "unknown": 0}
    update = store.update_closed_check

    cards = [m for m in store.list_matches() if m.get("status") in _ACTIVE_STATUSES]
    if not cards:
        return tally

    now = now_iso or datetime.now(timezone.utc).isoformat()
    factory = client_factory or (lambda: httpx.AsyncClient(follow_redirects=True))
    sem = asyncio.Semaphore(concurrency)

    async with factory() as client:
        async def _one(card: dict) -> tuple[dict, str]:
            async with sem:
                return card, await check_posting(client, card)

        results = await asyncio.gather(*(_one(c) for c in cards))

    for card, verdict in results:
        tally["checked"] += 1
        misses = int(card.get("closed_misses", 0) or 0)
        flagged = card.get("posting_closed_at") is not None
        try:
            if verdict == "miss":
                tally["misses"] += 1
                new_misses = misses + 1
                if new_misses >= 2 and not flagged:
                    update(card["job_id"], misses=new_misses, closed_at=now)
                    tally["flagged"] += 1
                else:
                    update(card["job_id"], misses=new_misses,
                           closed_at=card.get("posting_closed_at"))
            elif verdict == "alive":
                if misses or flagged:
                    update(card["job_id"], misses=0, closed_at=None)
                    tally["reset"] += 1
            else:
                tally["unknown"] += 1
        except Exception as exc:  # noqa: BLE001 — one card's write must not stop the sweep
            log.warning(
                "closed_sweep_update_failed",
                extra={"job_id": card.get("job_id"), "error": str(exc)},
            )

    log.info("closed_sweep_done", extra=tally)
    return tally
