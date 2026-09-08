"""Eightfold.ai job-board connector.

Eightfold tenants expose an unauthenticated JSON API at
``https://{slug}.eightfold.ai/api/{pcsx,apply/v2}``. Each tenant runs one of
two product generations (pcsx / apply_v2); the connector is told which via
config and falls back to the other on a 403 flavor-error. The list returns
coarse fields; enrich() scrapes the job page's schema.org JSON-LD (reusing
src/connectors/jsonld)."""
from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime, timezone

import httpx

from src.connectors.jsonld import _MAX_DESC, _jobposting_from_jsonld
from src.connectors.workday import _strip_html
from src.models import ConnectorState, FetchResult, NormalizedPosting, RawPosting
from src.user_agent import headers as ua_headers

log = logging.getLogger(__name__)

_PAGE_SIZE = 10   # Eightfold hard-caps num at 10
_MAX_PAGES = 10   # ~100 newest jobs; sort_by=timestamp puts survivors first
def _headers() -> dict[str, str]:
    return ua_headers(Accept="application/json")


class _FlavorMismatch(Exception):
    """The stored flavor's endpoint returned a 403 flavor-error; try the other."""


def _parse_epoch(ts: object) -> datetime | None:
    """Epoch seconds or millis → UTC datetime; anything else → None."""
    try:
        n = float(ts)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if n > 1e12:
        n /= 1000.0
    try:
        return datetime.fromtimestamp(n, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


class EightfoldConnector:
    tier = "ats"
    supports_enrich = True

    def __init__(self, slug: str, domain: str, flavor: str = "pcsx",
                 company: str | None = None) -> None:
        self.slug = slug
        self.domain = domain
        self.flavor = flavor
        self.company = company
        self.name = f"eightfold:{slug}"

    @property
    def _base(self) -> str:
        return f"https://{self.slug}.eightfold.ai"

    def _to_raw(self, pos: dict) -> RawPosting | None:
        pid = str(pos.get("id") or "").strip()
        title = (pos.get("name") or "").strip()
        if not pid or not title:
            return None
        locs = pos.get("locations")
        if isinstance(locs, list) and locs:
            location = "; ".join(str(x) for x in locs if x) or None
        else:
            location = (pos.get("location") or None)
        canonical = pos.get("canonicalPositionUrl")
        rel = pos.get("positionUrl")
        apply_url = canonical or (f"{self._base}{rel}" if rel else f"{self._base}/careers/job/{pid}")
        posted = _parse_epoch(pos.get("postedTs"))
        if posted is None:
            posted = _parse_epoch(pos.get("t_create"))
        return RawPosting(
            source=self.name, external_id=pid, title=title, description="",
            apply_url=apply_url, location=location, company=self.company, posted_at=posted,
            raw=pos,
        )

    async def _fetch_flavor(self, client: httpx.AsyncClient, flavor: str) -> list[RawPosting]:
        if flavor == "pcsx":
            path, pos_key, count_key = "/api/pcsx/search", ("data", "positions"), ("data", "count")
            extra = "&sort_by=timestamp"
        else:
            path, pos_key, count_key = "/api/apply/v2/jobs", ("positions",), ("count",)
            extra = ""
        out: list[RawPosting] = []
        seen_ids: set[str] = set()
        offset = 0
        fetched = 0  # raw item count seen so far, pre-dedupe (pcsx pages can overlap)
        total: int | None = None
        for page in range(_MAX_PAGES):
            url = (f"{self._base}{path}?domain={self.domain}"
                   f"&start={offset}&num={_PAGE_SIZE}{extra}")
            try:
                resp = await client.get(url, headers=_headers(), timeout=20.0)
                if resp.status_code == 403 and page == 0:
                    body = resp.text.lower()
                    if any(k in body for k in ("pcsx", "not enabled", "not authorized")):
                        raise _FlavorMismatch(flavor)
                resp.raise_for_status()
                data = resp.json()
            except _FlavorMismatch:
                raise
            except Exception:
                if page == 0:
                    raise  # first-page failure → connector failed this cycle
                log.warning("eightfold_page_failed", extra={"source": self.name, "page": page})
                break
            positions = _dig(data, pos_key) or []
            if not positions:
                break
            if total is None:
                total = _dig(data, count_key)
            fetched += len(positions)
            for pos in positions:
                raw = self._to_raw(pos)
                if raw is None or raw.external_id in seen_ids:
                    continue
                seen_ids.add(raw.external_id)
                out.append(raw)
            offset += _PAGE_SIZE
            if total is not None and fetched >= int(total):
                break
        return out

    async def fetch(self, client: httpx.AsyncClient, state: ConnectorState) -> FetchResult:
        order = [self.flavor, "apply_v2" if self.flavor == "pcsx" else "pcsx"]
        for i, flavor in enumerate(order):
            try:
                postings = await self._fetch_flavor(client, flavor)
            except _FlavorMismatch:
                if i == 0:
                    log.info("eightfold_flavor_fallback",
                             extra={"source": self.name, "from": flavor, "to": order[1]})
                    continue
                raise
            return FetchResult(postings=postings, new_state=None, not_modified=False)
        return FetchResult(postings=[], new_state=None, not_modified=False)

    async def enrich(self, client: httpx.AsyncClient, posting: NormalizedPosting) -> NormalizedPosting:
        pid = posting.job_id.rsplit(":", 1)[-1]
        url = f"{self._base}/careers/job/{pid}?domain={self.domain}"
        try:
            resp = await client.get(url, headers=_headers(), timeout=20.0)
            resp.raise_for_status()
            jp = _jobposting_from_jsonld(resp.text)
        except Exception as exc:  # noqa: BLE001 — enrich is best-effort
            log.warning("eightfold_enrich_failed",
                        extra={"source": self.name, "job_id": posting.job_id, "error": str(exc)})
            return posting
        if not jp:
            return posting
        desc = _strip_html(str(jp.get("description") or ""))[:_MAX_DESC]
        if not desc:
            return posting
        return replace(posting, description=desc)


def _dig(data: dict, keys: tuple[str, ...]):
    cur = data
    for k in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur
