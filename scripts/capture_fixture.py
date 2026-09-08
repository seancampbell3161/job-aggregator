#!/usr/bin/env python3
"""Capture a live ATS payload to tests/fixtures/.

Usage:
    python scripts/capture_fixture.py greenhouse stripe
    python scripts/capture_fixture.py lever netflix
    python scripts/capture_fixture.py ashby ramp
    python scripts/capture_fixture.py workable example

Manually verify the JSON before committing.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.user_agent import headers as ua_headers  # noqa: E402

FIXTURE_DIR = ROOT / "tests" / "fixtures"


def _greenhouse(slug: str) -> dict:
    r = httpx.get(
        f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
        params={"content": "true"}, headers=ua_headers(), timeout=30.0,
    )
    r.raise_for_status()
    return r.json()


def _lever(slug: str) -> dict | list:
    r = httpx.get(f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json"}, headers=ua_headers(), timeout=30.0)
    r.raise_for_status()
    return r.json()


def _ashby(slug: str) -> dict:
    r = httpx.get(
        f"https://api.ashbyhq.com/posting-api/job-board/{slug}",
        params={"includeCompensation": "true"}, headers=ua_headers(), timeout=30.0,
    )
    r.raise_for_status()
    return r.json()


def _workable(slug: str) -> dict:
    r = httpx.post(
        f"https://apply.workable.com/api/v3/accounts/{slug}/jobs",
        json={}, headers=ua_headers(), timeout=30.0,
    )
    r.raise_for_status()
    return r.json()


_FETCHERS = {
    "greenhouse": _greenhouse,
    "lever": _lever,
    "ashby": _ashby,
    "workable": _workable,
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", choices=sorted(_FETCHERS))
    parser.add_argument("slug")
    args = parser.parse_args()

    data = _FETCHERS[args.source](args.slug)
    out = FIXTURE_DIR / f"{args.source}_{args.slug}.json"
    out.write_text(json.dumps(data, indent=2))
    print(f"Wrote {out} ({out.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
