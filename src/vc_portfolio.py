"""Unified VC-portfolio discovery: drivers emit PortfolioCompany records, which
flow through a two-stage pipeline (slug-probe primary + fingerprint residual)
and merge via src.fingerprint. Two callers share the drivers: the manual CLI
(scripts/import_vc_portfolio.py) runs the full probe pipeline here, offline;
the daily discovery tier (src.discovery.run_vc_discovery) fetches the same
drivers weekly to stage probe-free candidate rows.
"""
from __future__ import annotations

import asyncio
import csv
import html
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

import httpx

from src.discovery import _SUPPORTED_ATS, _probe_one_ats
from src.fingerprint import FingerprintResult, Seed, connector_name, fingerprint_company
from src.user_agent import headers as ua_headers


# Suffixes stripped from company names before slug derivation. Lowercase.
_STRIP_TOKENS = {
    "corp", "corporation", "inc", "llc", "ltd", "limited",
    "technologies", "tech", "labs", "ai", "the", "co",
    "company", "group", "holdings", "io",
}


@dataclass(frozen=True)
class PortfolioCompany:
    """One portfolio company from a VC driver. `domain` (bare host, e.g.
    'netris.io') is optional — present enables the Stage-2 fingerprint pass."""
    name: str
    slug_candidates: list[str]
    domain: str | None = None


def name_slugs(name: str) -> list[str]:
    """Generate slug candidates from a human-readable company name.

    Returns most-likely-first; the pipeline probes them in order and keeps
    the first that resolves on any startup ATS. Both no-separator
    ("huggingface") and hyphen-separated ("hugging-face") variants are
    included for multi-word names since both conventions exist in ATS slugs.
    """
    parts = [p for p in re.split(r"[^A-Za-z0-9]+", name) if p]
    parts = [p for p in parts if p.lower() not in _STRIP_TOKENS]
    if not parts:
        safe = re.sub(r"[^a-z0-9]+", "", name.lower())
        return [safe or "_unknown"]
    no_sep = "".join(parts).lower()
    hyphen = "-".join(parts).lower()
    out = [no_sep]
    if hyphen != no_sep:
        out.append(hyphen)
    return out


def _bare_domain(url: str) -> str | None:
    """Bare host (no scheme/www/path/port) of a URL, or None if not a domain."""
    if not url:
        return None
    host = (urlparse(url).netloc or urlparse("//" + url).netloc).lower().split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    return host if ("." in host and "/" not in host and host) else None


def _slug_hit_to_result(
    company: PortfolioCompany, ats: str, slug: str, count: int
) -> FingerprintResult:
    """Normalize a slug-probe hit into a matched FingerprintResult so it merges
    through the same path as fingerprint results."""
    return FingerprintResult(
        name=company.name, domain=company.domain or "", status="matched",
        family=ats, identity={"slug": slug}, posting_count=count,
    )


# ---- a16z driver ----

# a16z's labels mix *vertical* (what the company does) and *stage* (Seed,
# Growth). Only the vertical matters for profile fit; a company is in-profile
# iff Active AND carries at least one in-profile vertical (compound labels like
# "American Dynamism;Enterprise" pass on the Enterprise). Stage labels ignored.
_A16Z_VERTICAL_IN_PROFILE = (
    "Enterprise",
    "Infra",
    "Fintech",
    "Consumer",
)


def a16z_in_profile(company: dict) -> bool:
    status = (company.get("status") or "").lower()
    if status != "active":
        return False
    verticals_raw = company.get("verticals") or ""
    if not verticals_raw:
        return False
    labels = [v.strip() for v in verticals_raw.split(";") if v.strip()]
    return any(lab in _A16Z_VERTICAL_IN_PROFILE for lab in labels)


async def _fetch_a16z_raw(client: httpx.AsyncClient) -> list[dict]:
    """The a16z portfolio page embeds the full company list as an HTML-entity-
    escaped JSON blob in `<div ... data-companies="...">`."""
    resp = await client.get(
        "https://a16z.com/portfolio/", headers=ua_headers(), timeout=20.0
    )
    resp.raise_for_status()
    m = re.search(r'data-companies="([^"]+)"', resp.text)
    if not m:
        raise RuntimeError(
            "a16z portfolio page missing data-companies attr; page structure changed"
        )
    companies = json.loads(html.unescape(m.group(1)))
    if not isinstance(companies, list):
        raise RuntimeError("a16z data-companies decoded to a non-list; page structure changed")
    return companies


async def a16z_portfolio(client: httpx.AsyncClient) -> list[PortfolioCompany]:
    raw = await _fetch_a16z_raw(client)
    out: list[PortfolioCompany] = []
    for c in raw:
        if not a16z_in_profile(c):
            continue
        name = c.get("name") or c.get("post_title") or ""
        if not name or name == "[untitled]":
            continue
        domain = _bare_domain(c.get("company_url") or c.get("url") or c.get("external_url") or "")
        permalink = c.get("permalink") or ""
        m = re.search(r"/companies/([^/]+)/?", permalink)
        primary = [m.group(1)] if m else []
        slugs = primary + [s for s in name_slugs(name) if s not in primary]
        if not slugs:
            continue
        out.append(PortfolioCompany(name=name, slug_candidates=slugs, domain=domain))
    return out


# ---- sequoia driver ----

_SEQUOIA_API = "https://sequoiacap.com/wp-json/wp/v2/company"
_SEQUOIA_MAX_PAGES = 10  # per_page=100 → up to 1000 companies (412 today)


async def sequoia_portfolio(client: httpx.AsyncClient) -> list[PortfolioCompany]:
    """Sequoia's WordPress REST API exposes a `company` post type. Paginate via
    the X-WP-TotalPages header. Records carry name + slug; no domain (ACF fields
    aren't REST-exposed), so these companies are slug-probe only."""
    out: list[PortfolioCompany] = []
    page = 1
    while page <= _SEQUOIA_MAX_PAGES:
        resp = await client.get(
            _SEQUOIA_API, params={"per_page": 100, "page": page},
            headers=ua_headers(), timeout=20.0,
        )
        if resp.status_code >= 400:
            if page == 1:
                raise RuntimeError(
                    f"sequoia {_SEQUOIA_API} returned {resp.status_code}; API blocked or moved"
                )
            print(f"note: sequoia page {page} returned {resp.status_code}; "
                  f"returning {len(out)} companies collected so far")
            break
        records = resp.json()
        if not records:
            break
        for r in records:
            name = html.unescape(((r.get("title") or {}).get("rendered") or "").strip())
            if not name:
                continue
            wp_slug = (r.get("slug") or "").strip()
            slugs = ([wp_slug] if wp_slug else []) + [s for s in name_slugs(name) if s != wp_slug]
            if not slugs:
                continue
            out.append(PortfolioCompany(name=name, slug_candidates=slugs, domain=None))
        total_pages = int(resp.headers.get("X-WP-TotalPages") or 1)
        if page >= total_pages:
            break
        page += 1
    return out


# ---- manual CSV driver ----


def csv_portfolio(path: Path) -> list[PortfolioCompany]:
    """Read a hand-curated `name[,domain]` CSV. A leading `name`/`company`
    header row is skipped; blank lines are ignored. No profile filter — the
    human curated the list."""
    out: list[PortfolioCompany] = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.reader(f):
            if not row or not row[0].strip():
                continue
            name = row[0].strip()
            if name.lower() in ("name", "company"):
                continue
            domain = _bare_domain(row[1].strip()) if len(row) > 1 and row[1].strip() else None
            out.append(PortfolioCompany(name=name, slug_candidates=name_slugs(name), domain=domain))
    return out


# ---- orchestration ----

FIRMS: tuple[str, ...] = ("a16z", "sequoia", "csv")
_SLUG_CONCURRENCY = 10
_FP_CONCURRENCY = 5


def _all_slugs_polled(company: PortfolioCompany, manual: set[str]) -> bool:
    """True iff every candidate slug is already queued — skip to avoid double-cover."""
    return bool(company.slug_candidates) and all(s in manual for s in company.slug_candidates)


async def _run_driver(
    firm: str, client: httpx.AsyncClient, csv_path: Path | None
) -> list[PortfolioCompany]:
    if firm == "a16z":
        return await a16z_portfolio(client)
    if firm == "sequoia":
        return await sequoia_portfolio(client)
    if firm == "csv":
        if csv_path is None:
            raise SystemExit("firm 'csv' requires --csv PATH")
        return csv_portfolio(csv_path)
    raise SystemExit(f"unknown firm '{firm}'; choices: {', '.join(FIRMS)}")


async def _slug_probe(company: PortfolioCompany, client: httpx.AsyncClient) -> FingerprintResult | None:
    """Stage 1: try each candidate slug against every startup ATS; first hit wins."""
    for slug in company.slug_candidates:
        for ats in _SUPPORTED_ATS:
            ok, count = await _probe_one_ats(client=client, ats_family=ats, slug=slug)
            if ok:
                return _slug_hit_to_result(company, ats, slug, count)
    return None


async def discover_portfolio(
    firm: str, *, client: httpx.AsyncClient, manual_companies: Iterable[str] = (),
    limit: int | None = None, only: str | None = None, csv_path: Path | None = None,
) -> list[FingerprintResult]:
    """Two-stage VC discovery → deduped matched FingerprintResults, ready for
    merge_results_into_settings. Stage 1 slug-probe (primary); Stage 2
    fingerprint the residual companies that have a domain. Companies whose
    every slug candidate is in ``manual_companies`` (discovery.manual_companies)
    are skipped."""
    companies = await _run_driver(firm, client, csv_path)
    if only:
        needle = only.lower()
        companies = [c for c in companies if needle in c.name.lower()]
    manual = set(manual_companies)
    companies = [c for c in companies if not _all_slugs_polled(c, manual)]
    if limit is not None:
        companies = companies[:limit]

    # Stage 1: slug-probe
    slug_sem = asyncio.Semaphore(_SLUG_CONCURRENCY)

    async def _stage1(c: PortfolioCompany):
        async with slug_sem:
            return c, await _slug_probe(c, client)

    results: list[FingerprintResult] = []
    residual: list[PortfolioCompany] = []
    for c, hit in await asyncio.gather(*(_stage1(c) for c in companies)):
        if hit is not None:
            results.append(hit)
        elif c.domain:
            residual.append(c)

    # Stage 2: fingerprint the residual-with-domain
    fp_sem = asyncio.Semaphore(_FP_CONCURRENCY)

    async def _stage2(c: PortfolioCompany):
        async with fp_sem:
            return await fingerprint_company(client, Seed(name=c.name, domain=c.domain))

    for r in await asyncio.gather(*(_stage2(c) for c in residual)):
        if r.status == "matched":
            results.append(r)

    # Dedup by connector identity (a slug and a fingerprint can land the same board)
    seen: set[str] = set()
    deduped: list[FingerprintResult] = []
    for r in results:
        key = connector_name(r)
        if key not in seen:
            seen.add(key)
            deduped.append(r)
    return deduped
