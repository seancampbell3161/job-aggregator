"""Enterprise ATS fingerprint discovery CLI.

Usage:
  uv run python scripts/discover_enterprise.py                  # dry-run report
  uv run python scripts/discover_enterprise.py --limit 25       # first 25 seeds
  uv run python scripts/discover_enterprise.py --only amex,ge   # name substring filter
  uv run python scripts/discover_enterprise.py --merge          # write matched into config.yaml

Dry-run first, review the report, then re-run with --merge. Merge appends
validated entries to config.yaml (comments preserved), deduped against
existing config + discovered slugs + suppressed connectors. Rollout after a
merge: config is bind-mounted -> `docker compose restart poller`. Each
enterprise board adds roughly 2-5s to the ats cycle — after merging a large
batch, check /pipeline cycle duration before merging more."""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.fingerprint import (
    DEFAULT_SEEDS,
    fingerprint_many,
    gather_already_polled,
    load_seeds,
    merge_results_into_config,
    _store_names_fail_soft,
    format_report,
)

DEFAULT_CONFIG = REPO_ROOT / "config.yaml"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="discover_enterprise", description=__doc__)
    ap.add_argument("--seed-file", type=Path, default=DEFAULT_SEEDS)
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--only", type=str, default=None, help="comma-separated name substrings")
    ap.add_argument("--merge", action="store_true", help="write matched entries into config.yaml")
    ap.add_argument("--out", type=Path, default=None, help="results JSON path")
    args = ap.parse_args(argv)

    seeds = load_seeds(args.seed_file)
    if args.only:
        needles = [s.strip().lower() for s in args.only.split(",") if s.strip()]
        seeds = [s for s in seeds if any(n in s.name.lower() for n in needles)]
    if args.limit is not None:
        seeds = seeds[: args.limit]
    print(f"Sweeping {len(seeds)} companies from {args.seed_file} ...")

    results = asyncio.run(fingerprint_many(seeds))

    out_path = args.out or REPO_ROOT / f"fingerprint-results-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.json"
    out_path.write_text(json.dumps([dataclasses.asdict(r) for r in results], indent=2))
    print(format_report(results))
    print(f"\nFull results: {out_path}")

    if args.merge:
        already = gather_already_polled(args.config) | _store_names_fail_soft()
        added, diff = merge_results_into_config(results, config_path=args.config, already_polled=already)
        if added:
            print(f"\nMerged {added} new entries into {args.config}:\n{diff}")
            print("\nRollout: config is bind-mounted -> `docker compose restart poller`.")
            print("Each enterprise board adds ~2-5s/cycle — check /pipeline duration after a large batch.")
        else:
            print("\nNothing new to merge (all matched entries already polled).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
