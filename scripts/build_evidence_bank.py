"""Thin regen: Jira CSV export -> evidence.json skeleton.

Groups tickets by an epic/project column into evidence-bank projects, turning
each ticket into an `achievement` carrying its issue key as the ticket_ref.
`metrics` (the curated numbers worth claiming) are LEFT EMPTY — that is judgment
work the user does by hand afterwards. Mechanical part only, so a refreshed CSV
is one command.

    python -m scripts.build_evidence_bank jira-export.csv --out resume/evidence.json
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path


def build_bank(rows: list[dict], *, group_by: str, key_col: str, summary_col: str) -> dict:
    groups: dict[str, list[dict]] = {}
    for r in rows:
        g = (r.get(group_by) or "").strip()
        if not g:
            continue  # ungrouped tickets are skipped (no project to attach to)
        groups.setdefault(g, []).append(r)

    projects = []
    for key in sorted(groups):
        achievements = [
            {"text": (r.get(summary_col) or "").strip(),
             "ticket_refs": [(r.get(key_col) or "").strip()]}
            for r in groups[key]
        ]
        projects.append({"key": key, "summary": key, "metrics": [], "achievements": achievements})
    return {"projects": projects}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m scripts.build_evidence_bank")
    ap.add_argument("csv_path", help="Jira CSV export")
    ap.add_argument("--out", default="resume/evidence.json")
    ap.add_argument("--group-by", default="Epic Link")
    ap.add_argument("--key-col", default="Issue key")
    ap.add_argument("--summary-col", default="Summary")
    args = ap.parse_args(argv)

    with open(args.csv_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    bank = build_bank(rows, group_by=args.group_by, key_col=args.key_col, summary_col=args.summary_col)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(bank, indent=2, ensure_ascii=False))
    print(f"wrote {args.out}: {len(bank['projects'])} projects from {len(rows)} tickets")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
