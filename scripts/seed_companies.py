#!/usr/bin/env python3.12
"""Bulk-add a curated list of company slugs to config.yaml.

Idempotent: re-running is safe — slugs already present are skipped.

Usage:
    python scripts/seed_companies.py              # add all curated companies
    python scripts/seed_companies.py --dry-run    # print what would be added
    python scripts/seed_companies.py --ats greenhouse  # only that ATS

Confidence notes
----------------
Only slugs verified against the live ATS boards (or known with high confidence)
are listed here.  When uncertain, we omit rather than guess — the user can add
manually via scripts/add_company.sh.

Workable slugs are left empty by default: Workable company slugs are often
abbreviated or custom and are harder to verify without checking each board URL.
"""

import argparse
import sys
from pathlib import Path

import yaml

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
    # Add manually: scripts/add_company.sh workable <slug>
    "workable": [],
}

REPO_ROOT = Path(__file__).parent.parent
CONFIG_PATH = REPO_ROOT / "config.yaml"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="Print actions without modifying config.yaml")
    parser.add_argument("--ats", metavar="NAME", help="Only seed this ATS (e.g. greenhouse)")
    args = parser.parse_args()

    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)

    sources: dict = cfg.setdefault("sources", {})

    counts: dict[str, int] = {}
    skipped_total = 0
    to_add: dict[str, list[str]] = {}

    ats_keys = [args.ats] if args.ats else list(CURATED.keys())

    for ats in ats_keys:
        if ats not in CURATED:
            print(f"ERROR: '{ats}' is not in the curated list. Valid options: {', '.join(CURATED)}", file=sys.stderr)
            sys.exit(1)

        slugs = CURATED[ats]
        existing: list[str] = sources.setdefault(ats, [])
        new_slugs = [s for s in slugs if s not in existing]
        skipped = len(slugs) - len(new_slugs)
        skipped_total += skipped
        counts[ats] = len(new_slugs)
        to_add[ats] = new_slugs

        for slug in new_slugs:
            print(f"  {'[dry-run] ' if args.dry_run else ''}Add {ats}:{slug}")

    total_added = sum(counts.values())
    detail = ", ".join(f"{ats}: {n}" for ats, n in counts.items() if n or args.ats)
    print(f"Added {total_added} ({detail}); skipped {skipped_total} already present")

    if args.dry_run:
        return

    # Apply changes
    for ats, new_slugs in to_add.items():
        existing = sources[ats]
        existing.extend(new_slugs)

    with open(CONFIG_PATH, "w") as f:
        yaml.safe_dump(cfg, f, sort_keys=False)


if __name__ == "__main__":
    main()
