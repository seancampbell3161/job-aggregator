"""Board candidates to verify: Common Crawl URLs → (family, identity), plus a
Lever supplement guessed from company names (Lever blocks crawlers, so it is
nearly absent from Common Crawl)."""
from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlparse

import httpx

from src.fingerprint import parse_ats_url, parse_eightfold_url, parse_jsonld_url, parse_taleo_url
from src.slugging import slug_candidates
from src.user_agent import headers as ua_headers
from src.vc_portfolio import a16z_portfolio, sequoia_portfolio
from src.yc_oss import _FEED_URL as YC_FEED_URL
from src.yc_oss import YcCompany, filter_companies, parse_companies

log = logging.getLogger(__name__)

SOURCE_CC = "commoncrawl"
SOURCE_LEVER = "lever-guess"
_GREENHOUSE_HOSTS = frozenset({"boards.greenhouse.io", "job-boards.greenhouse.io"})
# Path words the vendors use for their own pages, never a company board.
_JUNK = frozenset({"embed", "api", "jobs", "job", "careers", "static", "assets", "search",
                   "robots.txt", "favicon.ico", "sitemap.xml"})
_CLEAN = re.compile(r"^[\w.\-]+$")
_CHECKED_FIELDS = ("slug", "tenant", "site", "section", "region")
_PARSERS = (parse_ats_url, parse_taleo_url, parse_eightfold_url, parse_jsonld_url)
_US_REGIONS = frozenset({"United States of America", "America / Canada"})


@dataclass(frozen=True)
class Candidate:
    family: str
    identity: dict = field(hash=False, compare=False)
    source: str = SOURCE_CC
    company_hint: str | None = None

    @property
    def key(self) -> str:
        return identity_key(self.family, self.identity)


def identity_key(family: str, identity: dict) -> str:
    """Case-insensitive dedup/cache key: hosts are case-insensitive and the
    slug APIs we poll treat casing variants as one board."""
    return f"{family}:" + json.dumps({k: str(v).lower() for k, v in identity.items()},
                                     sort_keys=True)


def _checked(family: str, identity: dict) -> Candidate | None:
    for k in _CHECKED_FIELDS:
        v = identity.get(k)
        if v is None:
            continue
        if not isinstance(v, str) or not _CLEAN.match(v):
            return None
        # Reserved words only disqualify a vendor-level slug; a Workday site
        # may legitimately be called "Careers" or "jobs".
        if k == "slug" and v.lower() in _JUNK:
            return None
    return Candidate(family, dict(identity))


def candidate_from_url(url: str) -> Candidate | None:
    try:
        u = urlparse(url)
        host = (u.hostname or "").lower()
    except ValueError:
        return None
    if host in _GREENHOUSE_HOSTS and u.path.startswith("/embed/"):
        slug = (parse_qs(u.query).get("for") or [None])[0]
        return _checked("greenhouse", {"slug": slug}) if slug else None
    for parse in _PARSERS:
        try:
            hit = parse(url)
        except ValueError:
            return None
        if hit:
            return _checked(*hit)
    return None


def dedup(cands: Iterable[Candidate]) -> list[Candidate]:
    """First-seen candidate per key, in key order (stable across runs)."""
    seen: dict[str, Candidate] = {}
    for c in cands:
        seen.setdefault(c.key, c)
    return [seen[k] for k in sorted(seen)]


def lever_guesses(companies: Iterable[tuple[str, str | None]]) -> list[Candidate]:
    out: list[Candidate] = []
    for name, website in companies:
        for slug in slug_candidates(name, website, None):
            c = _checked("lever", {"slug": slug})
            if c is not None:
                out.append(Candidate("lever", c.identity, SOURCE_LEVER, company_hint=name))
    return out


def yc_us_companies(companies: list[YcCompany], *,
                    min_team_size: int = 5) -> list[tuple[str, str | None]]:
    return [(c.name, c.website) for c in filter_companies(companies, min_team_size=min_team_size)
            if c.name and _US_REGIONS & set(c.regions)]


async def supplement_companies(client: httpx.AsyncClient, *,
                               min_team_size: int = 5) -> list[tuple[str, str | None]]:
    """(name, website) for US YC companies plus the a16z and Sequoia portfolios.
    The YC feed is required; a failing VC driver only shrinks the supplement."""
    resp = await client.get(YC_FEED_URL, headers=ua_headers(), timeout=60.0)
    resp.raise_for_status()
    out = yc_us_companies(parse_companies(resp.json()), min_team_size=min_team_size)
    for driver in (a16z_portfolio, sequoia_portfolio):
        try:
            out += [(c.name, f"https://{c.domain}" if c.domain else None)
                    for c in await driver(client)]
        except Exception as exc:  # noqa: BLE001 — optional source
            log.warning("vc_driver_failed", extra={"driver": driver.__name__, "error": str(exc)})
    return out
