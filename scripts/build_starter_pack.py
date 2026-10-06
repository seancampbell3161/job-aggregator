"""Build the bundled starter pack from public data: Common Crawl's URL index
plus a Lever supplement guessed from YC/a16z/Sequoia company names.

    uv run python scripts/build_starter_pack.py [--crawls 4] [--crawl CC-MAIN-2026-39 ...]
        [--cache-dir ~/.cache/job-aggregator/pack-build] [--max-age-days 7]
        [--out scripts/seeds/starter_pack.json] [--version YYYY-MM-DD]
        [--report PATH] [--allow-partial] [--no-lever]

Release tooling (RELEASING.md step 2); the app never runs it. A full build
checks ~40k candidates and takes about an hour; re-runs reuse fresh results
from the cache. Exit codes: 0 written, 1 refused (partial or empty), 2 Common
Crawl unreachable or no valid crawl id (or a usage error)."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.pack_builder.cache import BuildCache  # noqa: E402
from scripts.pack_builder.candidates import (  # noqa: E402
    SOURCE_CC, Candidate, candidate_from_url, dedup, lever_guesses, supplement_companies,
)
from scripts.pack_builder.commoncrawl import (  # noqa: E402
    SURT_PREFIXES, CrawlFetchError, crawl_urls, list_crawls,
)
from scripts.pack_builder.select import LiveBoard, select, to_pack  # noqa: E402
from scripts.pack_builder.verify import Verifier  # noqa: E402
from src.starter_pack import STARTER_PACK_PATH, load_pack, write_pack  # noqa: E402
from src.user_agent import headers as ua_headers  # noqa: E402

class SupplementError(Exception):
    """The company-name feed (YC) was unreachable or unreadable."""


PARTIAL_THRESHOLD = 0.05
_BATCH = 500
# Bump when a cached candidate list's shape or meaning changes (for example
# how candidate_from_url normalises identities), so old lists are not reused.
CRAWL_CACHE_VERSION = 1
# Crawl ids come from a remote collinfo.json and name a cache file.
_CRAWL_ID = re.compile(r"^CC-MAIN-\d{4}-\d{2}$")


def crawl_id(value: str) -> str:
    """argparse type for --crawl."""
    if not _CRAWL_ID.match(value):
        raise argparse.ArgumentTypeError(f"not a Common Crawl id (CC-MAIN-YYYY-WW): {value!r}")
    return value


def _valid_crawl_ids(ids: list) -> list[str]:
    valid: list[str] = []
    for crawl in ids:
        if isinstance(crawl, str) and _CRAWL_ID.match(crawl):
            valid.append(crawl)
        else:
            print(f"warning: skipping invalid Common Crawl id {crawl!r}", file=sys.stderr)
    return valid


def _crawl_cache_name(crawl: str) -> str:
    """{crawl}.{hash8}.v{N}.json. The prefixes searched decide which
    candidates a crawl yields, so adding a family must miss the cache."""
    digest = hashlib.sha256("\n".join(SURT_PREFIXES).encode()).hexdigest()[:8]
    return f"{crawl}.{digest}.v{CRAWL_CACHE_VERSION}.json"


async def gather_candidates(client, crawl_ids: list[str], cache_dir: Path, *,
                            lever: bool) -> tuple[list[Candidate], dict]:
    """Candidates from every crawl (cached per crawl once it read cleanly)
    plus the Lever supplement, deduped. Returns (candidates, crawl stats)."""
    stats: dict = {}
    found: list[Candidate] = []
    crawl_dir = cache_dir / "crawls"
    crawl_dir.mkdir(parents=True, exist_ok=True)
    for crawl in crawl_ids:
        cached = crawl_dir / _crawl_cache_name(crawl)
        pairs = None
        if cached.exists():
            try:
                pairs = json.loads(cached.read_text())
            except ValueError:  # corrupt cache file: treat as a miss and re-read
                pairs = None
        if pairs is not None:
            stats[crawl] = {"cached": True, "candidates": len(pairs), "failed_blocks": 0}
        else:
            urls, failed = await crawl_urls(client, crawl, SURT_PREFIXES)
            cands = [c for c in map(candidate_from_url, urls) if c is not None]
            pairs = [[c.family, c.identity] for c in dedup(cands)]
            if failed == 0:
                cached.write_text(json.dumps(pairs))
            stats[crawl] = {"cached": False, "urls": len(urls), "candidates": len(pairs),
                            "failed_blocks": failed}
        found += [Candidate(f, i, SOURCE_CC) for f, i in pairs]
    if lever:
        try:
            companies = await supplement_companies(client)
        except (httpx.HTTPError, ValueError) as exc:
            raise SupplementError(str(exc) or type(exc).__name__) from exc
        found += lever_guesses(companies)
    return dedup(found), stats


async def _verify_all(verifier: Verifier, cache: BuildCache, cands: list[Candidate],
                      max_age_days: int) -> None:
    pending = [c for c in cands if cache.needs_check(c.key, max_age_days=max_age_days)]
    print(f"verifying {len(pending)} of {len(cands)} candidates "
          f"({len(cands) - len(pending)} fresh in cache)", flush=True)
    for start in range(0, len(pending), _BATCH):
        batch = pending[start:start + _BATCH]
        results = await asyncio.gather(*(verifier.check(c) for c in batch))
        for c, r in zip(batch, results):
            cache.put(c.key, c.family, r)
        print(f"  {min(start + _BATCH, len(pending))}/{len(pending)}", flush=True)


async def build(args, client) -> int:
    try:
        crawl_ids = _valid_crawl_ids(args.crawl or await list_crawls(client, args.crawls))
        if not crawl_ids:
            print("error: no valid Common Crawl id to read", file=sys.stderr)
            return 2
        cands, crawl_stats = await gather_candidates(client, crawl_ids, args.cache_dir,
                                                     lever=not args.no_lever)
    except CrawlFetchError as exc:
        print(f"error: Common Crawl unreachable: {exc}", file=sys.stderr)
        return 2
    except SupplementError as exc:
        print(f"error: company-name supplement unavailable ({exc}); "
              "re-run later or pass --no-lever", file=sys.stderr)
        return 2
    for crawl, st in crawl_stats.items():
        if st.get("failed_blocks"):
            print(f"warning: {crawl}: {st['failed_blocks']} index blocks could not be read; "
                  "its candidates are incomplete and it was not cached", file=sys.stderr)
    cache = BuildCache(args.cache_dir / "checks.db")
    try:
        await _verify_all(Verifier(client), cache, cands, args.max_age_days)
        results = cache.results(c.key for c in cands)
    finally:
        cache.close()

    status: dict[str, Counter] = defaultdict(Counter)
    reasons: dict[str, Counter] = defaultdict(Counter)
    for family, r in results.values():
        status[family][r.status] += 1
        if r.status != "live":
            reasons[family][r.reason or "unknown"] += 1
    deferred = sum(s["deferred"] for s in status.values())
    picks = select(LiveBoard(k, fam, r) for k, (fam, r) in results.items() if r.status == "live")
    pack = to_pack(picks, args.version)

    previous = None
    if args.out.exists():
        try:
            old = load_pack(args.out)
            previous = {"slugs": len(old.slugs), "boards": len(old.boards)}
        except Exception:  # noqa: BLE001 — an unreadable old pack just has no baseline
            previous = None
    report = {
        "version": args.version,
        "crawls": crawl_stats,
        "candidates": dict(Counter(c.source for c in cands)),
        "status": {f: dict(c) for f, c in sorted(status.items())},
        "reasons": {f: dict(c) for f, c in sorted(reasons.items())},
        "deferred_ratio": round(deferred / len(cands), 4) if cands else 0.0,
        "selected": {region: {"boards": len(p.boards), "postings": p.postings,
                              "binding": p.binding} for region, p in picks.items()},
        "previous": previous,
    }
    if args.report:
        Path(args.report).write_text(json.dumps(report, indent=1, sort_keys=True) + "\n")
    print(json.dumps(report["selected"], sort_keys=True))
    if previous is not None:
        print(f"previous pack: {previous['slugs']} slugs + {previous['boards']} boards")

    if cands and deferred / len(cands) > PARTIAL_THRESHOLD and not args.allow_partial:
        print(f"error: {deferred} of {len(cands)} checks deferred (> {PARTIAL_THRESHOLD:.0%}); "
              "re-run later to retry them, or pass --allow-partial", file=sys.stderr)
        return 1
    if not pack["slugs"] and not pack["boards"]:
        print("error: no live boards selected; refusing to write an empty pack", file=sys.stderr)
        return 1
    write_pack(pack, args.out)
    print(f"wrote {len(pack['slugs'])} slugs + {len(pack['boards'])} boards to {args.out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--crawls", type=int, default=4, help="newest N crawls (default 4)")
    ap.add_argument("--crawl", action="append", type=crawl_id,
                    help="explicit crawl id (CC-MAIN-YYYY-WW); repeatable")
    ap.add_argument("--cache-dir", type=Path,
                    default=Path.home() / ".cache" / "job-aggregator" / "pack-build")
    ap.add_argument("--max-age-days", type=int, default=7)
    ap.add_argument("--out", type=Path, default=STARTER_PACK_PATH)
    ap.add_argument("--version", default=date.today().isoformat())
    ap.add_argument("--report", type=Path)
    ap.add_argument("--allow-partial", action="store_true")
    ap.add_argument("--no-lever", action="store_true", help="skip the Lever name-guess supplement")
    args = ap.parse_args(argv)

    async def run() -> int:
        # No follow_redirects: connectors must see exactly what the app's poll
        # client sees (handler.py builds a plain AsyncClient). Personio, for
        # one, reads a 3xx as "not a tenant". commoncrawl._get opts in per request.
        async with httpx.AsyncClient(headers=ua_headers(), timeout=30.0) as client:
            return await build(args, client)

    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())
