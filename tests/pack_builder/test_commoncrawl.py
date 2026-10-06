import gzip
import json

import httpx
import pytest
import respx

from scripts.pack_builder.commoncrawl import (
    COLLINFO_URL, DATA_BASE, Block, CrawlFetchError, crawl_urls, list_crawls,
    parse_cluster_idx, select_blocks, urls_from_block,
)

KEYS = ["a,aaa)/", "com,ashbyhq,jobs)/acme", "com,ashbyhq,jobs)/zeta", "com,b)/", "io,greenhouse,boards)/x"]


async def _no_sleep(_s):
    return None


def test_parse_cluster_idx_strips_timestamp_and_skips_bad_lines():
    keys, blocks = parse_cluster_idx([
        "com,a)/ 20260901\tcdx-00001.gz\t100\t50\t7",
        "garbage",
        "com,b)/ 20260902\tcdx-00001.gz\tnot-a-number\t50\t8",
    ])
    assert keys == ["com,a)/"] and blocks == [Block("cdx-00001.gz", 100, 50)]


def test_select_blocks_includes_the_block_a_prefix_starts_inside():
    # Block 0 spans [a,aaa)/ .. com,ashbyhq,jobs)/acme): jobs.ashbyhq.com keys may start in it.
    assert list(select_blocks(KEYS, "com,ashbyhq,jobs)/")) == [0, 1, 2]


def test_select_blocks_prefix_inside_one_block():
    assert list(select_blocks(KEYS, "com,ashbyhq,jobs)/m")) == [1]


def test_select_blocks_prefix_after_every_key_is_the_last_block():
    assert list(select_blocks(KEYS, "zz,")) == [4]


def test_select_blocks_prefix_before_every_key_is_empty():
    assert list(select_blocks(KEYS, "0,")) == []


def test_urls_from_block_filters_by_prefix_and_tolerates_junk():
    text = "\n".join([
        'com,ashbyhq,jobs)/acme 2026 {"url": "https://jobs.ashbyhq.com/acme", "status": "200"}',
        'com,ashbyhq,jobs)/beta 2026 {not json',
        'com,other)/ 2026 {"url": "https://other.com/"}',
    ])
    assert list(urls_from_block(text, "com,ashbyhq,jobs)/")) == ["https://jobs.ashbyhq.com/acme"]


@pytest.mark.asyncio
async def test_list_crawls_retries_then_returns_newest_n():
    with respx.mock:
        route = respx.get(COLLINFO_URL)
        route.side_effect = [httpx.Response(504),
                             httpx.Response(200, json=[{"id": "CC-3"}, {"id": "CC-2"}, {"id": "CC-1"}])]
        async with httpx.AsyncClient() as client:
            assert await list_crawls(client, 2, sleep=_no_sleep) == ["CC-3", "CC-2"]


@pytest.mark.asyncio
async def test_list_crawls_gives_up_with_crawl_fetch_error():
    with respx.mock:
        respx.get(COLLINFO_URL).respond(504)
        async with httpx.AsyncClient() as client:
            with pytest.raises(CrawlFetchError):
                await list_crawls(client, 1, sleep=_no_sleep)


def _block(rows: list[str]) -> bytes:
    return gzip.compress("\n".join(rows).encode())


@pytest.mark.asyncio
async def test_crawl_urls_reads_only_matching_blocks():
    idx = "\n".join([
        "com,ashbyhq,jobs)/acme 2026\tcdx-00001.gz\t0\t10\t1",
        "com,zzz)/ 2026\tcdx-00002.gz\t0\t10\t2",
    ])
    block = _block(['com,ashbyhq,jobs)/acme 2026 {"url": "https://jobs.ashbyhq.com/acme"}'])
    with respx.mock:
        respx.get(f"{DATA_BASE}/CC-1/indexes/cluster.idx").respond(200, text=idx)
        b1 = respx.get(f"{DATA_BASE}/CC-1/indexes/cdx-00001.gz").respond(206, content=block)
        b2 = respx.get(f"{DATA_BASE}/CC-1/indexes/cdx-00002.gz").respond(206, content=b"")
        async with httpx.AsyncClient() as client:
            urls, failed = await crawl_urls(client, "CC-1", ("com,ashbyhq,jobs)/",), sleep=_no_sleep)
    assert urls == ["https://jobs.ashbyhq.com/acme"] and failed == 0
    assert b1.called and not b2.called
    assert b1.calls[0].request.headers["Range"] == "bytes=0-9"


@pytest.mark.asyncio
async def test_crawl_urls_counts_undecodable_block_as_failed():
    idx = "com,ashbyhq,jobs)/acme 2026\tcdx-00001.gz\t0\t10\t1"
    with respx.mock:
        respx.get(f"{DATA_BASE}/CC-1/indexes/cluster.idx").respond(200, text=idx)
        respx.get(f"{DATA_BASE}/CC-1/indexes/cdx-00001.gz").respond(206, text="<html>oops</html>")
        async with httpx.AsyncClient() as client:
            urls, failed = await crawl_urls(client, "CC-1", ("com,ashbyhq,jobs)/",), sleep=_no_sleep)
    assert urls == [] and failed == 1


@pytest.mark.asyncio
async def test_crawl_urls_rejects_200_response_to_range_request():
    """A Range request that returns 200 (server ignoring Range) fails the block."""
    idx = "com,ashbyhq,jobs)/acme 2026\tcdx-00001.gz\t0\t10\t1"
    block = _block(['com,ashbyhq,jobs)/acme 2026 {"url": "https://jobs.ashbyhq.com/acme"}'])
    with respx.mock:
        respx.get(f"{DATA_BASE}/CC-1/indexes/cluster.idx").respond(200, text=idx)
        respx.get(f"{DATA_BASE}/CC-1/indexes/cdx-00001.gz").respond(200, content=block)
        async with httpx.AsyncClient() as client:
            urls, failed = await crawl_urls(client, "CC-1", ("com,ashbyhq,jobs)/",), sleep=_no_sleep)
    assert urls == [] and failed == 1


@pytest.mark.asyncio
async def test_crawl_urls_counts_corrupt_gzip_as_failed():
    """A gzip block with valid header but corrupted deflate stream raises zlib.error and counts as failed."""
    idx = "com,ashbyhq,jobs)/acme 2026\tcdx-00001.gz\t0\t10\t1"
    # Create a valid gzip block, then corrupt the deflate stream (not header).
    # Flipping byte 10 triggers zlib.error (not BadGzipFile/OSError) during decompression.
    valid_gzip = gzip.compress(b"test data for compression" * 10)
    corrupt = bytearray(valid_gzip)
    corrupt[10] = 0xFF
    corrupt_gzip = bytes(corrupt)
    with respx.mock:
        respx.get(f"{DATA_BASE}/CC-1/indexes/cluster.idx").respond(200, text=idx)
        respx.get(f"{DATA_BASE}/CC-1/indexes/cdx-00001.gz").respond(206, content=corrupt_gzip)
        async with httpx.AsyncClient() as client:
            urls, failed = await crawl_urls(client, "CC-1", ("com,ashbyhq,jobs)/",), sleep=_no_sleep)
    assert urls == [] and failed == 1


@pytest.mark.asyncio
async def test_list_crawls_handles_malformed_collinfo():
    """If collinfo.json is not the expected format, raise CrawlFetchError."""
    with respx.mock:
        respx.get(COLLINFO_URL).respond(200, text="<html>error page</html>")
        async with httpx.AsyncClient() as client:
            with pytest.raises(CrawlFetchError):
                await list_crawls(client, 1, sleep=_no_sleep)
