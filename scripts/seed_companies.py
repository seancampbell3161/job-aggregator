#!/usr/bin/env python3.12
"""Bulk-add a curated list of company slugs to the settings sources.

Idempotent: re-running is safe — slugs already present are skipped.

Usage:
    python scripts/seed_companies.py              # add all curated companies
    python scripts/seed_companies.py --dry-run    # print what would be added
    python scripts/seed_companies.py --ats greenhouse  # only that ATS

Confidence notes
----------------
Only slugs verified against the live ATS boards (or known with high confidence)
are listed here.  When uncertain, we omit rather than guess — the user can add
manually via `python -m src.settings add-source <ats> <slug>`.

Workable slugs are left empty by default: Workable company slugs are often
abbreviated or custom and are harder to verify without checking each board URL.
"""

import argparse
import sys
from pathlib import Path

# Curated dict: {ats_name: [slug, ...]}
# Greenhouse slugs: verified via https://boards-api.greenhouse.io/v1/boards/<slug>/jobs
# Lever slugs:      verified via https://api.lever.co/v0/postings/<slug>
# Ashby slugs:      verified via https://api.ashbyhq.com/posting-api/job-board/<slug>
CURATED: dict[str, list[str]] = {
    "greenhouse": [
        "airbnb",
        "anthropic",
        "brex",
        "coinbase",
        "databricks",
        "discord",
        "doordash",
        "dropbox",
        "duolingo",
        "figma",
        "gitlab",
        "hashicorp",
        "instacart",
        "lyft",
        "notion",
        "openai",
        "pinterest",
        "plaid",
        "ramp",
        "reddit",
        "robinhood",
        "roblox",
        "segment",
        "snap",
        "snowflake",
        "stripe",
        "twilio",
    ],
    "lever": [
        "cohere",
        "mixpanel",
        "netflix",
        "palantir",
        "scaleai",
        "shopify",
        "spotify",
    ],
    "ashby": [
        "linear",
        "modal",
        "posthog",
        "replicate",
        "resend",
        "vercel",
    ],
    # Workable slugs are omitted — too easy to guess wrong.
    # Add manually: python -m src.settings add-source workable <slug>
    "workable": [],
}

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="Print actions without saving settings")
    parser.add_argument("--ats", metavar="NAME", help="Only seed this ATS (e.g. greenhouse)")
    args = parser.parse_args(argv)

    ats_keys = [args.ats] if args.ats else list(CURATED)
    for ats in ats_keys:
        if ats not in CURATED:
            print(f"ERROR: '{ats}' is not in the curated list. Valid options: {', '.join(CURATED)}",
                  file=sys.stderr)
            return 1

    from src.settings import EXPORT_TIP, HOST_WRITE_WARNING, open_service
    from src.settings.errors import NotConfigured, StaleWrite
    from src.settings.sources import append_slug_sources

    service = open_service()
    snap = service.snapshot()
    if snap is None:
        print("ERROR: not set up — import settings first: python -m src.settings import DIR",
              file=sys.stderr)
        return 1

    counts: dict[str, int] = {}
    skipped_total = 0
    for ats in ats_keys:
        existing = set(getattr(snap.cfg.sources, ats))
        new_slugs = [s for s in dict.fromkeys(CURATED[ats]) if s not in existing]
        skipped_total += len(set(CURATED[ats])) - len(new_slugs)
        counts[ats] = len(new_slugs)
        for slug in new_slugs:
            print(f"  {'[dry-run] ' if args.dry_run else ''}Add {ats}:{slug}")

    total = sum(counts.values())
    detail = ", ".join(f"{ats}: {n}" for ats, n in counts.items() if n or args.ats)
    print(f"Added {total} ({detail}); skipped {skipped_total} already present")
    if args.dry_run or total == 0:
        return 0
    print(HOST_WRITE_WARNING, file=sys.stderr)
    try:
        append_slug_sources(service, {ats: CURATED[ats] for ats in ats_keys}, label="seed_companies")
    except (NotConfigured, StaleWrite) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(EXPORT_TIP)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
