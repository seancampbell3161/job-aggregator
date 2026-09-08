"""Probe ATS endpoints for a company name; print which ATS family + slug works.

Usage:
    python scripts/discover.py <slug-guess>
        e.g. python scripts/discover.py stripe

This is a manual companion to the autodiscovery routine — useful when adding a
specific company you've heard about, or sanity-checking discovery output.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass

import httpx
from src.user_agent import headers as ua_headers


@dataclass(frozen=True)
class ProbeResult:
    ats_family: str
    slug: str
    posting_count: int


async def _probe_one(client: httpx.AsyncClient, ats_family: str, slug: str) -> ProbeResult | None:
    url_for = {
        "greenhouse": (f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", "GET", "jobs"),
        "lever": (f"https://api.lever.co/v0/postings/{slug}", "GET", None),  # returns a list
        "ashby": (f"https://api.ashbyhq.com/posting-api/job-board/{slug}", "GET", "jobs"),
        "workable": (f"https://apply.workable.com/api/v3/accounts/{slug}/jobs", "POST", "results"),
        "smartrecruiters": (f"https://api.smartrecruiters.com/v1/companies/{slug}/postings", "GET", "content"),
    }
    url, method, list_key = url_for[ats_family]
    try:
        if method == "POST":
            resp = await client.post(url, json={}, headers=ua_headers(), timeout=20.0)
        else:
            resp = await client.get(url, headers=ua_headers(), timeout=20.0)
        if resp.status_code >= 400:
            return None
        data = resp.json()
        if list_key is None and isinstance(data, list):
            count = len(data)
        elif isinstance(data, dict) and isinstance(data.get(list_key or ""), list):
            count = len(data[list_key])
        else:
            count = 0
        return ProbeResult(ats_family=ats_family, slug=slug, posting_count=count)
    except Exception:  # noqa: BLE001 — manual tool, swallow errors per ATS
        return None


async def probe_company(slug: str, *, client: httpx.AsyncClient) -> list[ProbeResult]:
    """Probe each supported ATS family for the given slug.

    Returns matching ATSs ordered by posting count (descending)."""
    families = ["greenhouse", "lever", "ashby", "workable", "smartrecruiters"]
    results = await asyncio.gather(*(_probe_one(client, ats, slug) for ats in families))
    matched = [r for r in results if r is not None]
    matched.sort(key=lambda r: r.posting_count, reverse=True)
    return matched


async def _main(slug: str) -> int:
    async with httpx.AsyncClient() as client:
        results = await probe_company(slug, client=client)
    if not results:
        print(f"No ATS family responded for slug '{slug}'.")
        print("Try variations like 'companyname', 'companyname-careers', 'companynamejobs'.")
        return 1
    print(f"Matches for '{slug}':")
    for r in results:
        print(f"  {r.ats_family:18s} {r.posting_count:5d} postings")
    print()
    print(f"To add to config.yaml: append '{results[0].slug}' to sources.{results[0].ats_family}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("slug", help="Slug to probe (try variations of the company name)")
    args = parser.parse_args()
    return asyncio.run(_main(args.slug))


if __name__ == "__main__":
    raise SystemExit(main())
