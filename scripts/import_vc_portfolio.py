#!/usr/bin/env python3
"""Import VC-portfolio companies into settings via the unified two-stage
discovery pipeline (slug-probe + fingerprint residual). Offline manual tool —
run locally, review the dry-run report, then re-run with --merge.

    uv run python scripts/import_vc_portfolio.py a16z               # dry-run report
    uv run python scripts/import_vc_portfolio.py a16z --merge       # save into settings
    uv run python scripts/import_vc_portfolio.py csv --csv lightspeed.csv --merge

Library: src/vc_portfolio.py.
"""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import httpx  # noqa: E402

from src.vc_portfolio import FIRMS, discover_portfolio  # noqa: E402
from src.user_agent import headers as ua_headers  # noqa: E402
from src.fingerprint import (  # noqa: E402
    merge_results_into_settings,
    _store_names_fail_soft,
    format_report,
)
from src.settings import open_service  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="import_vc_portfolio", description=__doc__)
    ap.add_argument("firm", choices=FIRMS)
    ap.add_argument("--csv", type=Path, default=None, help="name[,domain] CSV for firm 'csv'")
    ap.add_argument("--merge", action="store_true", help="save matched entries into settings")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--only", type=str, default=None, help="name substring filter")
    ap.add_argument("--out", type=Path, default=None, help="results JSON path")
    args = ap.parse_args(argv)

    service = open_service()
    snap = service.snapshot()
    if args.merge and snap is None:
        print("not set up — import settings first: python -m src.settings import DIR", file=sys.stderr)
        return 1
    manual = set(snap.cfg.discovery.manual_companies) if snap is not None else set()

    async def _run() -> list:
        async with httpx.AsyncClient(follow_redirects=True, headers=ua_headers()) as client:
            return await discover_portfolio(
                args.firm, client=client, manual_companies=manual,
                limit=args.limit, only=args.only, csv_path=args.csv,
            )

    results = asyncio.run(_run())
    print(f"{args.firm}: {len(results)} matched boards")
    print(format_report(results))

    if args.out:
        args.out.write_text(json.dumps([dataclasses.asdict(r) for r in results], indent=2))

    if args.merge:
        added, summary = merge_results_into_settings(
            service, results, label=f"import_vc_portfolio {args.firm}",
            already_polled=_store_names_fail_soft(),
        )
        if added:
            print(f"\nMerged {added} new entries into settings (applies live):\n{summary}")
        else:
            print("\nNothing new to merge (all matched entries already polled).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
