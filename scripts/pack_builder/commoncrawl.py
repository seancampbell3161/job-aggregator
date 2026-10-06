"""Read job-board URLs out of Common Crawl's static URL index.

index.commoncrawl.org (the query server) 504s under load, so lookups never go
through it; only its tiny collinfo.json crawl list is read from there. Each
crawl publishes a sorted summary, cluster.idx (~105 MB), whose lines point at
~3,000-capture gzip blocks inside cdx-NNNNN.gz on data.commoncrawl.org. We
pick the blocks whose key range can hold a host's SURT prefix and fetch just
those byte ranges, the same thing the query server does internally."""
from __future__ import annotations

import asyncio
import bisect
import gzip
import json
import logging
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

import httpx

from src.user_agent import headers as ua_headers

log = logging.getLogger(__name__)

DATA_BASE = "https://data.commoncrawl.org/cc-index/collections"
COLLINFO_URL = "https://index.commoncrawl.org/collinfo.json"
# SURT host prefixes for every board host the app can poll. "x,y,z)/" is one
# host; "x,y," is every subdomain (tenant hosts).
SURT_PREFIXES: tuple[str, ...] = (
    "io,greenhouse,boards)/", "io,greenhouse,job-boards)/",
    "com,ashbyhq,jobs)/", "co,lever,jobs)/",
    "com,smartrecruiters,jobs)/", "com,smartrecruiters,careers)/",
    "com,workable,apply)/", "com,rippling,ats)/",
    "com,myworkdayjobs,", "de,personio,jobs,", "com,personio,jobs,",
    "com,recruitee,", "com,teamtailor,", "com,oraclecloud,",
    "net,taleo,", "ai,eightfold,", "com,icims,",
)
_FETCH_CONCURRENCY = 8
_ATTEMPTS = 5


class CrawlFetchError(RuntimeError):
    """A Common Crawl file could not be fetched after retries."""


@dataclass(frozen=True)
class Block:
    file: str
    offset: int
    length: int


def parse_cluster_idx(lines: Iterable[str]) -> tuple[list[str], list[Block]]:
    """cluster.idx lines ("<surt> <timestamp>\\t<file>\\t<offset>\\t<length>\\t<id>")
    → (sorted SURT keys, the block each key starts). Malformed lines are skipped."""
    keys: list[str] = []
    blocks: list[Block] = []
    for line in lines:
        parts = line.rstrip("\n").split("\t")
        if len(parts) < 4:
            continue
        try:
            offset, length = int(parts[2]), int(parts[3])
        except ValueError:
            continue
        keys.append(parts[0].split(" ", 1)[0])
        blocks.append(Block(parts[1], offset, length))
    return keys, blocks


def select_blocks(keys: list[str], prefix: str) -> range:
    """Indices of blocks that can hold a key starting with prefix. Block i
    holds keys in [keys[i], keys[i+1]), so the block before the first match
    counts too: the prefix may start inside it."""
    start = max(bisect.bisect_right(keys, prefix) - 1, 0)
    end = bisect.bisect_left(keys, prefix + "￿")
    return range(start, end)


def urls_from_block(text: str, prefix: str) -> Iterator[str]:
    """Captured URLs in a decompressed cdx block whose SURT key starts with prefix."""
    for row in text.splitlines():
        if not row.startswith(prefix):
            continue
        j = row.find("{")
        if j < 0:
            continue
        try:
            url = json.loads(row[j:])["url"]
        except (ValueError, KeyError, TypeError):
            continue
        if isinstance(url, str):
            yield url


async def _get(client: httpx.AsyncClient, url: str, *, headers: dict | None = None,
               sleep=asyncio.sleep) -> httpx.Response:
    last = "no attempt"
    for attempt in range(_ATTEMPTS):
        try:
            resp = await client.get(url, headers={**ua_headers(), **(headers or {})},
                                    timeout=120.0, follow_redirects=True)
            if resp.status_code in (200, 206):
                return resp
            last = f"HTTP {resp.status_code}"
            if resp.status_code < 500 and resp.status_code != 429:
                break  # a 4xx won't fix itself
        except httpx.HTTPError as exc:
            last = type(exc).__name__
        await sleep(3.0 * (attempt + 1))
    raise CrawlFetchError(f"{url}: {last}")


async def list_crawls(client: httpx.AsyncClient, n: int, *, sleep=asyncio.sleep) -> list[str]:
    """The newest n crawl ids, e.g. ["CC-MAIN-2026-39", ...]."""
    resp = await _get(client, COLLINFO_URL, sleep=sleep)
    return [c["id"] for c in resp.json()][:n]


async def crawl_urls(client: httpx.AsyncClient, crawl_id: str,
                     prefixes: tuple[str, ...] = SURT_PREFIXES, *,
                     sleep=asyncio.sleep) -> tuple[list[str], int]:
    """Every captured URL under prefixes in one crawl, plus how many blocks
    could not be fetched or decoded (the caller decides whether that's fatal).
    Raises CrawlFetchError when cluster.idx itself is unreachable."""
    idx = await _get(client, f"{DATA_BASE}/{crawl_id}/indexes/cluster.idx", sleep=sleep)
    keys, blocks = parse_cluster_idx(idx.text.splitlines())
    wanted: dict[int, list[str]] = {}
    for prefix in prefixes:
        for i in select_blocks(keys, prefix):
            wanted.setdefault(i, []).append(prefix)
    sem = asyncio.Semaphore(_FETCH_CONCURRENCY)
    failed = 0

    async def one(i: int) -> list[str]:
        nonlocal failed
        b = blocks[i]
        async with sem:
            try:
                resp = await _get(
                    client, f"{DATA_BASE}/{crawl_id}/indexes/{b.file}",
                    headers={"Range": f"bytes={b.offset}-{b.offset + b.length - 1}"}, sleep=sleep)
                text = gzip.decompress(resp.content).decode("utf-8", "replace")
            except (CrawlFetchError, OSError, EOFError) as exc:  # BadGzipFile is an OSError
                failed += 1
                log.warning("cc_block_failed",
                            extra={"crawl": crawl_id, "file": b.file, "error": str(exc)})
                return []
        return [u for prefix in wanted[i] for u in urls_from_block(text, prefix)]

    chunks = await asyncio.gather(*(one(i) for i in sorted(wanted)))
    return [u for chunk in chunks for u in chunk], failed
