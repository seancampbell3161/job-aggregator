"""Company-concentration report (read-only).

Answers "am I seeing the same companies over and over?" with numbers instead of
impressions, and compares a window against the equal-length window before it so
a change is visible rather than felt.

Run on the box whose SQLite DB holds the postings (production):

  uv run python scripts/concentration_report.py
  uv run python scripts/concentration_report.py --days 30
  uv run python scripts/concentration_report.py --days 14 --top 20

WATCH THE BASELINE. The first measurement (2026-07-27) reported 262 distinct
companies over 14 days, but it was taken while the hiring.cafe connector had
been dead for 13 days — that source alone contributes ~70 companies per cycle.
Any window overlapping 2026-07-14..2026-07-28 is depressed for that reason, and
comparing against it will overstate an improvement. The report flags windows
that overlap the outage.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = REPO_ROOT / "data" / "job_aggregator.db"

# hiring.cafe delivered nothing between these dates (Cloudflare challenge on the
# homepage, which was where the buildId was scraped). Windows overlapping this
# under-report diversity.
_OUTAGE = (datetime(2026, 7, 14, tzinfo=timezone.utc), datetime(2026, 7, 28, tzinfo=timezone.utc))


# Corporate-form suffixes stripped for GROUPING ONLY. Connectors that supply a
# company name directly (adzuna gives the legal name) disagree with ones that
# derive it, so "Microsoft" and "Microsoft Corporation" arrive as two employers
# and both land in the top 10 — inflating the company count and understating
# concentration, which are the two numbers this report exists to produce.
# src/normalize.py's fix covered slug-derived names, not this.
#
# Deliberately short and conservative: these are forms that never distinguish
# two real companies. Nothing here merges on a shared first word, which would
# be how you'd wrongly fuse unrelated employers.
_SUFFIXES = {"inc", "inc.", "incorporated", "corp", "corp.", "corporation",
             "llc", "l.l.c.", "ltd", "ltd.", "limited", "plc", "co", "co.",
             "company", "holdings", "group", "gmbh", "sa", "nv", "ab", "oy"}


def _canonical(name: str) -> str:
    """Grouping key for a company name. Display uses the raw spelling."""
    words = [w for w in name.lower().replace(",", " ").split() if w]
    while words and words[-1] in _SUFFIXES:
        words.pop()
    return " ".join(words) or name.lower()


def _blocked_companies() -> list[list[str]]:
    """filters.blocked_companies from config.yaml, tokenized. Best-effort — the
    report is still useful without it, so a missing or unreadable config is not
    an error."""
    try:
        import yaml
        raw = yaml.safe_load((REPO_ROOT / "config.yaml").read_text())
        return [e.lower().split() for e in (raw["filters"]["blocked_companies"] or []) if e]
    except Exception:  # noqa: BLE001 — config is optional context, not input
        return []


def _is_blocked(name: str, blocked: list[list[str]]) -> bool:
    """Same consecutive-run match as src/filters.filter_company, so this agrees
    with what the pipeline actually rejects."""
    toks = _canonical(name).split()
    return any(
        toks[i:i + len(e)] == e
        for e in blocked for i in range(len(toks) - len(e) + 1)
    )


def _window(conn, start: datetime, end: datetime) -> list[tuple[str, str]]:
    """(company, source) for every notified posting in [start, end)."""
    rows = conn.execute(
        "SELECT json_extract(data,'$.company'), json_extract(data,'$.source') "
        "FROM seen_jobs WHERE notified = 1 AND first_seen >= ? AND first_seen < ?",
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    return [(c, s or "") for c, s in rows if c]


def _stats(rows: list[tuple[str, str]], top_n: int) -> dict:
    companies = Counter(_canonical(c) for c, _ in rows)
    # Show the spelling that actually appeared most often, not the stripped key.
    display: dict[str, Counter] = {}
    for c, _ in rows:
        display.setdefault(_canonical(c), Counter())[c] += 1
    label = {k: v.most_common(1)[0][0] for k, v in display.items()}
    total = sum(companies.values())
    top = [(label[k], n) for k, n in companies.most_common(top_n)]
    return {
        "notifications": total,
        "companies": len(companies),
        # The headline number: how much of the feed the loudest employers own.
        "top_share": (sum(n for _, n in top) / total) if total else 0.0,
        "singletons": sum(1 for n in companies.values() if n == 1),
        "top": top,
        "families": Counter((s.split(":", 1)[0] or "?") for _, s in rows),
        "family_companies": {
            f: len({_canonical(c) for c, s in rows if (s.split(":", 1)[0] or "?") == f})
            for f in {(s.split(":", 1)[0] or "?") for _, s in rows}
        },
        "names": set(companies),
    }


def _delta(now: float, before: float, *, pct: bool = False, invert: bool = False) -> str:
    if not before:
        return "    n/a"
    change = (now - before) / before * 100
    good = (change < 0) if invert else (change > 0)
    arrow = "+" if change >= 0 else ""
    mark = "" if abs(change) < 1 else ("  ✓" if good else "  ✗")
    return f"{arrow}{change:5.1f}%{mark}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--days", type=int, default=14, help="window length (default 14)")
    ap.add_argument("--top", type=int, default=10, help="top-N companies (default 10)")
    args = ap.parse_args()

    db = Path(args.db)
    if not db.exists():
        print(f"no database at {db}", file=sys.stderr)
        return 1
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)

    now = datetime.now(timezone.utc)
    cur_start = now - timedelta(days=args.days)
    prev_start = cur_start - timedelta(days=args.days)

    cur = _stats(_window(conn, cur_start, now), args.top)
    prev = _stats(_window(conn, prev_start, cur_start), args.top)

    print(f"\nCONCENTRATION — {args.days}d to {now:%Y-%m-%d} "
          f"vs the {args.days}d before it\n" + "=" * 62)
    if not cur["notifications"]:
        print("no notified postings in the current window")
        return 0

    print(f"{'':<26}{'current':>10}{'previous':>11}{'change':>11}")
    rows = [
        ("notifications", cur["notifications"], prev["notifications"], False),
        ("distinct companies", cur["companies"], prev["companies"], False),
        (f"top-{args.top} share of feed",
         f"{cur['top_share']:.0%}", f"{prev['top_share']:.0%}", True),
        ("companies seen once", cur["singletons"], prev["singletons"], False),
    ]
    for label, a, b, invert in rows:
        an = float(str(a).rstrip("%")); bn = float(str(b).rstrip("%"))
        print(f"{label:<26}{str(a):>10}{str(b):>11}{_delta(an, bn, invert=invert):>11}")

    fresh = cur["names"] - prev["names"]
    print(f"{'employers new this window':<26}{len(fresh):>10}")

    blocked = _blocked_companies()
    print(f"\nTOP {args.top} EMPLOYERS")
    stale = 0
    for name, n in cur["top"]:
        if _is_blocked(name, blocked):
            stale += 1
            print(f"   {n:>4}  {name}   [denylisted — aging out of this window]")
        else:
            print(f"   {n:>4}  {name}")
    if stale:
        print(f"\n   {stale} of the top {args.top} are already on filters.blocked_companies.")
        print("   They stop arriving the day the denylist is applied, but stay in the")
        print("   window until it rolls past them — so concentration reads worse than")
        print("   it now is. Re-run once the window clears the date you added them.")

    print("\nDIVERSITY BY SOURCE FAMILY  (companies per notification — higher is broader)")
    for fam, n in cur["families"].most_common(12):
        cos = cur["family_companies"][fam]
        print(f"   {fam:<18} {n:>5} notifs  {cos:>4} companies   {cos / n:>5.2f}")

    if cur_start < _OUTAGE[1] and now > _OUTAGE[0]:
        print("\n⚠  This window overlaps the hiring.cafe outage (2026-07-14..07-28);")
        print("   diversity is under-reported. Re-run once the window clears it.")
    if prev_start < _OUTAGE[1] and cur_start > _OUTAGE[0]:
        print("\n⚠  The COMPARISON window overlaps the hiring.cafe outage, so any")
        print("   improvement shown above is partly just that source coming back.")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
