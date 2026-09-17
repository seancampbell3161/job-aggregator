import json

import httpx
import pytest
import respx

from src.digest import format_gap_digest, send_gap_digest, tally_gaps


def test_tally_ranks_by_frequency_and_drops_singletons():
    lists = [
        ["Kubernetes", "Kafka"],
        ["Kubernetes", "Redis"],
        ["Kubernetes"],
        ["Kafka"],
        ["Rust"],          # appears once → dropped
        [],                # clean match → ignored
    ]
    tally = tally_gaps(lists)
    assert tally == [("Kubernetes", 3), ("Kafka", 2)]


def test_tally_is_case_insensitive_with_display_form():
    lists = [["Kubernetes"], ["kubernetes"], ["KUBERNETES"]]
    tally = tally_gaps(lists)
    assert tally == [("Kubernetes", 3)]   # first-seen display form preserved


def test_tally_dedupes_within_a_single_job():
    lists = [["Go", "Go"], ["Go"]]
    tally = tally_gaps(lists)
    assert tally == [("Go", 2)]           # the doubled entry counts once


def test_tally_caps_at_top_10():
    # Each skill appears in 2 jobs (above the singleton threshold) → 15 qualifying
    # skills; result must be capped at 10.
    lists = [[f"skill{i}"] for i in range(15)] + [[f"skill{i}"] for i in range(15)]
    tally = tally_gaps(lists)
    assert len(tally) == 10


def test_tally_min_count_one_keeps_singletons():
    lists = [["Rust"], ["Go"], ["Go"]]
    # min_count=1 surfaces singletons (for the analytics UI's early-stage view)
    assert tally_gaps(lists, min_count=1) == [("Go", 2), ("Rust", 1)]
    # default still drops singletons (the weekly digest's behavior is unchanged)
    assert tally_gaps(lists) == [("Go", 2)]


def test_tally_top_param_caps_results():
    # "a" has a strictly higher count so the cap is deterministic by value,
    # not by tie-break ordering.
    lists = [["a"], ["a"], ["a"], ["b"], ["b"], ["c"], ["c"]]
    assert tally_gaps(lists, top=1) == [("a", 3)]


def test_format_digest_with_results():
    out = format_gap_digest([("Kubernetes", 7), ("Kafka", 4)], window_days=30, total_jobs=42)
    assert "42 matches" in out
    assert "30 days" in out
    assert "Kubernetes (7)" in out
    assert "Kafka (4)" in out


def test_format_digest_no_matches():
    out = format_gap_digest([], window_days=30, total_jobs=0)
    assert "no matches" in out.lower()


def test_format_digest_matches_but_no_recurring_gaps():
    out = format_gap_digest([], window_days=30, total_jobs=12)
    assert "12 matches" in out
    assert "no recurring" in out.lower()


@pytest.mark.asyncio
async def test_send_gap_digest_posts_content():
    async with httpx.AsyncClient() as client:
        with respx.mock:
            route = respx.post("https://discord.test/webhook").respond(204)
            await send_gap_digest(client, "https://discord.test/webhook", "hello digest")
        body = json.loads(route.calls.last.request.content.decode())
        assert body["content"] == "hello digest"


@pytest.mark.asyncio
async def test_send_gap_digest_truncates_to_2000():
    async with httpx.AsyncClient() as client:
        with respx.mock:
            route = respx.post("https://discord.test/webhook").respond(204)
            await send_gap_digest(client, "https://discord.test/webhook", "x" * 5000)
        body = json.loads(route.calls.last.request.content.decode())
        assert len(body["content"]) == 2000


@pytest.mark.asyncio
async def test_send_gap_digest_raises_on_5xx():
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.post("https://discord.test/webhook").respond(500)
            with pytest.raises(httpx.HTTPStatusError):
                await send_gap_digest(client, "https://discord.test/webhook", "x")
