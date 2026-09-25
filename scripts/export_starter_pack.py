"""Export a long-running instance's verified boards as the bundled starter pack.

    python scripts/export_starter_pack.py --db data/job_aggregator.db \
        [--discovered-only] [--out scripts/seeds/starter_pack.json] [--version YYYY-MM-DD]

Run it against the production box's SQLite file (copy it off the box, or run
inside the container), review the diff, and commit. Read-only on the DB."""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.fingerprint import EU_SEEDS, load_seeds  # noqa: E402
from src.settings.service import ConfigService  # noqa: E402
from src.settings.store import SqliteSettingsStore  # noqa: E402
from src.sqlite_db import connect  # noqa: E402
from src.starter_pack import BOARD_FAMILIES, ORIGIN_US, SLUG_FAMILIES, STARTER_PACK_PATH  # noqa: E402
from src.state_sqlite import (  # noqa: E402
    SqliteConnectorHealthStore, SqliteDiscoveredBoardsStore, SqliteDiscoveredSlugsStore,
)

_EU_FAMILIES = frozenset({"personio", "recruitee", "teamtailor"})


def _region(family: str, domain: str | None, eu_domains: set[str]) -> str:
    return "eu" if family in _EU_FAMILIES or (domain and domain in eu_domains) else "us"


def _is_starter(origin: str | None) -> bool:
    return bool(origin) and origin.startswith(ORIGIN_US)


def _config_boards(cfg) -> list[tuple[str, str, dict, str | None]]:
    """(family, connector_name, identity, company) for configured structured boards."""
    s = cfg.sources
    out = []
    for w in s.workday:
        out.append(("workday", f"workday:{w.tenant}:{w.site}",
                    {"tenant": w.tenant, "region": w.region, "site": w.site}, None))
    for o in s.oraclecloud:
        out.append(("oraclecloud", f"oraclecloud:{o.tenant}:{o.site}",
                    {"tenant": o.tenant, "region": o.region, "site": o.site}, o.company))
    for t in s.taleo:
        out.append(("taleo", f"taleo:{t.tenant}:{t.section}",
                    {"tenant": t.tenant, "section": t.section}, t.company))
    for e in s.eightfold:
        out.append(("eightfold", f"eightfold:{e.slug}",
                    {"slug": e.slug, "domain": e.domain, "flavor": e.flavor}, e.company))
    for j in s.jsonld_boards:
        out.append(("jsonld", f"{j.family}:{j.slug}",
                    {"family": j.family, "slug": j.slug, "base_url": j.base_url}, j.company))
    return out


def build_pack(conn, *, discovered_only: bool, version: str, eu_domains: set[str]) -> dict:
    health = SqliteConnectorHealthStore(conn)
    blocked = set(health.suppressed_names()) | set(health.backoff_names(int(time.time() * 1000)))
    slugs: dict[str, dict] = {}
    boards: dict[str, dict] = {}

    for r in SqliteDiscoveredSlugsStore(conn).list_healthy():
        if (r.ats_family not in SLUG_FAMILIES or _is_starter(r.origin)
                or r.last_posting_count <= 0 or r.connector_name in blocked):
            continue
        slugs[r.connector_name] = {
            "ats": r.ats_family, "slug": r.slug, "company": r.company_name,
            "region": _region(r.ats_family, None, eu_domains), "postings": r.last_posting_count,
        }
    for b in SqliteDiscoveredBoardsStore(conn).list_healthy():
        if (b.family not in BOARD_FAMILIES or not b.identity or not b.connector_name
                or _is_starter(b.origin) or b.connector_name in blocked):
            continue
        boards[b.connector_name] = {
            "family": b.family, "identity": b.identity, "company": b.company,
            "domain": b.domain, "connector_name": b.connector_name,
            "region": _region(b.family, b.domain, eu_domains), "postings": 0,
        }

    if not discovered_only:
        snap = ConfigService(SqliteSettingsStore(conn), env={}).snapshot()
        if snap is not None:
            for fam in sorted(SLUG_FAMILIES):
                for slug in getattr(snap.cfg.sources, fam, []) or []:
                    key = f"{fam}:{slug}"
                    if key in blocked or key in slugs:
                        continue
                    slugs[key] = {"ats": fam, "slug": slug, "company": None,
                                  "region": _region(fam, None, eu_domains), "postings": 0}
            for fam, key, identity, company in _config_boards(snap.cfg):
                if key in blocked or key in boards:
                    continue
                boards[key] = {"family": fam, "identity": identity, "company": company,
                               "domain": f"config:{key}", "connector_name": key,
                               "region": _region(fam, None, eu_domains), "postings": 0}

    return {
        "version": version,
        "slugs": sorted(slugs.values(), key=lambda s: (s["ats"], s["slug"])),
        "boards": sorted(boards.values(), key=lambda b: (b["family"], b["connector_name"])),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", required=True)
    ap.add_argument("--out", default=str(STARTER_PACK_PATH))
    ap.add_argument("--version", default=date.today().isoformat())
    ap.add_argument("--discovered-only", action="store_true")
    args = ap.parse_args(argv)
    conn = connect(args.db)
    eu_domains = {s.domain for s in load_seeds(EU_SEEDS)}
    pack = build_pack(conn, discovered_only=args.discovered_only,
                      version=args.version, eu_domains=eu_domains)
    Path(args.out).write_text(json.dumps(pack, indent=1, sort_keys=True) + "\n")
    print(f"wrote {len(pack['slugs'])} slugs + {len(pack['boards'])} boards to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
