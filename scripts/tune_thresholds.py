"""Verdict-driven threshold tuning report (read-only).

Reads the accumulated /audit rescue/confirm verdicts and prints suggested
adjustments to `relevance.score_low` and `filters.titles`. NEVER writes
config — copy the suggested lines into config.yaml by hand, then
`docker compose restart poller`. Run on the box whose SQLite DB holds the
verdicts (production).

  uv run python scripts/tune_thresholds.py
  uv run python scripts/tune_thresholds.py --since 2026-06-01 --min-verdicts 15
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import yaml

from src.tuning import (
    analyze_role_titles, analyze_score_low, analyze_snippet_pairs, analyze_source_scores,
)


def _iso_or_empty(s: str) -> str:
    if s == "":
        return s
    from datetime import datetime
    try:
        datetime.fromisoformat(s)
    except ValueError:
        raise argparse.ArgumentTypeError(f"--since must be an ISO date/datetime or empty, got {s!r}")
    return s


def _load_config(config_path: str) -> tuple[int, list[str]]:
    raw = yaml.safe_load(Path(config_path).read_text()) or {}
    score_low = int((raw.get("relevance") or {}).get("score_low", 4))
    titles = list((raw.get("filters") or {}).get("titles", []))
    return score_low, titles


def _read_verdict_rows(since_iso: str):
    """(suppressed_rows, rejected_rows) from the local sqlite stores, or
    ([], []) with a printed note if the DB isn't reachable. Also prints a
    note counting raw rows dropped for lacking a usable signal (no int
    score / no title), per the spec's skipped-row reporting promise."""
    try:
        from src.sqlite_db import connect
        from src.state_sqlite import SqliteRejectedPostingsStore, SqliteSeenJobsStore
        conn = connect()
        seen = SqliteSeenJobsStore(conn)
        rejected = SqliteRejectedPostingsStore(conn)
        suppressed = seen.list_suppressed_details(since_iso=since_iso)
        # Merge in score_low rescues that transitioned through /audit/rescue:
        # those rows are no longer "suppressed" (they're notified now), but
        # they carry the rescue verdict + the ORIGINAL suppressed score, so
        # analyze_score_low needs them alongside the still-suppressed rows.
        rescued = (
            seen.list_rescued_suppressions(since_iso=since_iso)
            if hasattr(seen, "list_rescued_suppressions") else []
        )
        suppressed = suppressed + rescued
        role_rejected = rejected.list_rejected(
            since_iso=since_iso, gate="role", include_judged=True, limit=100_000,
        )

        skipped_score = sum(
            1 for row in suppressed
            if not isinstance(row.get("score"), int) or isinstance(row.get("score"), bool)
        )
        skipped_title = sum(
            1 for row in role_rejected
            if row.get("rejected_by") == "role" and not (row.get("title") or "").strip()
        )
        if skipped_score or skipped_title:
            print(
                f"note: skipped {skipped_score} suppressed row(s) without an integer score, "
                f"{skipped_title} role row(s) without a title"
            )

        return suppressed, role_rejected
    except Exception as exc:  # noqa: BLE001 — read-only tool degrades gracefully
        print(f"note: could not read verdict stores ({type(exc).__name__}: {exc})")
        return [], []


def run(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Verdict-driven threshold tuning report (read-only).")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument(
        "--since", default="", type=_iso_or_empty,
        help="ISO date/datetime; only verdicts on/after are used",
    )
    ap.add_argument("--min-verdicts", type=int, default=10)
    args = ap.parse_args(argv)

    score_low, titles = _load_config(args.config)
    suppressed, rejected = _read_verdict_rows(args.since)

    sl = analyze_score_low(suppressed, current=score_low, min_verdicts=args.min_verdicts)
    role = analyze_role_titles(rejected, current_titles=titles)

    print("=== score_low ===")
    print(f"current: {score_low}   samples: {sl.sample_size} "
          f"(rescued {sl.rescued_scores}, confirmed {sl.confirmed_scores})")
    if sl.suggested is not None and sl.confident:
        print(f"error curve (threshold: misclassifications): {sl.errors_by_threshold}")
        print(f"note: {sl.note}")
        if sl.suggested != score_low:
            print(f"\n  suggested config change:\n    relevance:\n      score_low: {sl.suggested}\n")
        else:
            print("  suggested: no change")
    else:
        print(f"note: {sl.note}")

    print("\n=== role / title regex ===")
    print(f"note: {role.note}")
    if role.rescued_titles:
        print("rescued titles (regex false-negatives):")
        for t in role.rescued_titles:
            print(f"  - {t}")
    if role.suggested_tokens:
        print("candidate tokens/bigrams to cover in filters.titles (count):")
        for tok, n in role.suggested_tokens:
            print(f"  - {tok}  ({n})")
    if role.confirmed_count:
        print(f"({role.confirmed_count} role-rejected title(s) confirmed correctly rejected)")

    try:
        from src.sqlite_db import connect
        from src.state_sqlite import SqliteSeenJobsStore
        score_rows = SqliteSeenJobsStore(connect()).list_score_rows(since_iso=args.since)
    except Exception as exc:  # noqa: BLE001 — read-only tool degrades gracefully
        print(f"\nnote: could not read seen store ({type(exc).__name__}: {exc})")
        score_rows = []

    print("\n=== score by source ===")
    stats = analyze_source_scores(score_rows)
    if not stats:
        print("note: no scored rows yet")
    for s in stats:
        print(f"{s.source_family:<16} n={s.count:<5} median={s.median:.1f} "
              f"p25={s.p25:.1f} p75={s.p75:.1f} suppressed={s.suppressed_rate:.0%}")

    print("\n=== adzuna snippet pairs ===")
    pair_report = analyze_snippet_pairs(score_rows)
    print(f"note: {pair_report.note}")
    for pair in pair_report.pairs[:20]:
        print(f"  {pair.adzuna_job_id}  vs  {pair.other_job_id}: "
              f"{pair.adzuna_score} vs {pair.other_score} (delta {pair.delta:+d})")

    return 0  # insufficient data is a normal outcome, never an error


if __name__ == "__main__":
    raise SystemExit(run())
