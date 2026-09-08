"""yc-oss/api JSON dump client + filter + slug derivation.

The yc-oss project (https://github.com/yc-oss/api) publishes a community-
maintained JSON dump of YC companies on GitHub Pages. We pull the full file
each discovery run and filter to active companies above a team-size threshold.
yc-oss is static GitHub Pages content — no anti-bot, no auth, no rate limits."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx
from src.user_agent import headers as ua_headers

from src.slugging import normalize_name_slug

log = logging.getLogger(__name__)

_FEED_URL = "https://yc-oss.github.io/api/companies/all.json"
_INCLUDED_STATUSES = frozenset({"Active", "Public"})


@dataclass(frozen=True)
class YcCompany:
    """A single yc-oss company record. Only the fields we use."""
    name: str
    slug: str           # yc-oss internal slug; not necessarily the ATS slug
    status: str         # "Active" / "Public" / "Inactive" / "Acquired" / ...
    team_size: int      # 0 if missing in payload
    website: str | None


def parse_companies(payload: Any) -> list[YcCompany]:
    """Defensive parser. Returns [] on unrecognized shapes."""
    if not isinstance(payload, list):
        return []
    out: list[YcCompany] = []
    for c in payload:
        if not isinstance(c, dict):
            continue
        try:
            ts = c.get("team_size")
            if not isinstance(ts, (int, float)):
                ts = 0
            out.append(
                YcCompany(
                    name=str(c.get("name") or "").strip(),
                    slug=str(c.get("slug") or "").strip(),
                    status=str(c.get("status") or "").strip(),
                    team_size=int(ts),
                    website=(c.get("website") if isinstance(c.get("website"), str) else None),
                )
            )
        except Exception:  # noqa: BLE001 — defensive: one bad entry must not crash the batch
            log.exception("yc_oss_parse_entry_failed")
            continue
    return out


def filter_companies(companies: list[YcCompany], *, min_team_size: int) -> list[YcCompany]:
    return [
        c for c in companies
        if c.status in _INCLUDED_STATUSES and c.team_size >= min_team_size
    ]


def derive_slug(name: str) -> str:
    """Slugify a company display name. Thin wrapper over the conversion
    chain's variant-1 rule (src.slugging.normalize_name_slug) so this module's
    existing callers and the chain can never drift apart."""
    return normalize_name_slug(name)


def derive_slug_for(c: YcCompany) -> str:
    """Slug for a yc-oss record. Falls back to the yc-internal slug if the
    name is empty or slugifies to empty."""
    s = derive_slug(c.name)
    if s:
        return s
    return c.slug


class YcOssClient:
    """Fetches the yc-oss JSON dump and returns filtered records."""

    def __init__(self, *, min_team_size: int = 10) -> None:
        self._min_team_size = min_team_size

    async def fetch(self, client: httpx.AsyncClient) -> list[YcCompany]:
        try:
            resp = await client.get(_FEED_URL, headers=ua_headers(), timeout=30.0)
            if resp.status_code >= 400:
                log.warning(
                    "yc_oss_fetch_failed",
                    extra={"status_code": resp.status_code},
                )
                return []
            payload = resp.json()
        except Exception:  # noqa: BLE001 — yc-oss outage must not crash discovery
            log.exception("yc_oss_fetch_error")
            return []
        return filter_companies(parse_companies(payload), min_team_size=self._min_team_size)
