import json

import pytest

import scripts.build_starter_pack as B
from scripts.pack_builder.cache import CheckResult
from src.starter_pack import load_pack, write_pack

URLS = ["https://jobs.ashbyhq.com/acme", "https://boards.greenhouse.io/beta",
        "https://acme.wd1.myworkdayjobs.com/External"]


class FakeVerifier:
    calls = 0
    script: dict = {}

    def __init__(self, client, **kw):
        pass

    async def check(self, cand):
        FakeVerifier.calls += 1
        key = f"{cand.family}:{cand.identity.get('slug') or cand.identity.get('tenant')}"
        return FakeVerifier.script.get(key, CheckResult("dead"))


def _live(connector_name, us=3):
    slug = connector_name.split(":", 1)[1]
    return CheckResult("live", 5, us, 0, slug.title(), connector_name, {"slug": slug})


@pytest.fixture
def env(monkeypatch, tmp_path):
    FakeVerifier.calls = 0
    FakeVerifier.script = {            # workday:acme is absent → dead
        "ashby:acme": _live("ashby:acme"),
        "greenhouse:beta": _live("greenhouse:beta"),
    }
    crawl_calls = []

    async def fake_list(client, n, **kw):
        return ["CC-1"]

    async def fake_crawl(client, crawl_id, prefixes=None, **kw):
        crawl_calls.append(crawl_id)
        return list(URLS), 0

    async def fake_supplement(client, **kw):
        return []

    monkeypatch.setattr(B, "list_crawls", fake_list)
    monkeypatch.setattr(B, "crawl_urls", fake_crawl)
    monkeypatch.setattr(B, "supplement_companies", fake_supplement)
    monkeypatch.setattr(B, "Verifier", FakeVerifier)
    out = tmp_path / "pack.json"
    args = ["--cache-dir", str(tmp_path / "cache"), "--out", str(out),
            "--version", "2026-10-06", "--report", str(tmp_path / "report.json")]
    return out, args, crawl_calls, tmp_path


def test_happy_path_writes_a_loadable_pack_and_report(env):
    out, args, _, tmp = env
    assert B.main(args) == 0
    pack = load_pack(out)
    assert {s.connector_name for s in pack.slugs} == {"ashby:acme", "greenhouse:beta"}
    report = json.loads((tmp / "report.json").read_text())
    assert report["selected"]["us"]["boards"] == 2
    assert report["status"]["ashby"]["live"] == 1
    assert report["status"]["workday"]["dead"] == 1


def test_rerun_skips_fresh_cached_checks(env):
    out, args, _, _ = env
    B.main(args)
    first = FakeVerifier.calls
    B.main(args)
    assert FakeVerifier.calls == first        # 3 fresh results reused, nothing re-verified


def test_crawl_candidates_are_cached(env):
    _, args, crawl_calls, _ = env
    B.main(args)
    B.main(args)
    assert crawl_calls == ["CC-1"]


def test_crawl_with_failed_blocks_is_not_cached(env, monkeypatch):
    _, args, crawl_calls, _ = env

    async def flaky(client, crawl_id, prefixes=None, **kw):
        crawl_calls.append(crawl_id)
        return list(URLS), 2

    monkeypatch.setattr(B, "crawl_urls", flaky)
    B.main(args)
    B.main(args)
    assert crawl_calls == ["CC-1", "CC-1"]


def test_partial_build_is_refused_and_pack_untouched(env):
    out, args, _, _ = env
    out.write_text("ORIGINAL")
    FakeVerifier.script["ashby:acme"] = CheckResult("deferred")     # 1 of 3 = 33% > 5%
    assert B.main(args) == 1
    assert out.read_text() == "ORIGINAL"


def test_allow_partial_writes_anyway(env):
    out, args, _, _ = env
    FakeVerifier.script["ashby:acme"] = CheckResult("deferred")
    assert B.main(args + ["--allow-partial"]) == 0
    assert {s.connector_name for s in load_pack(out).slugs} == {"greenhouse:beta"}


def test_empty_pack_is_refused(env):
    out, args, _, _ = env
    FakeVerifier.script = {}
    assert B.main(args) == 1 and not out.exists()


def test_report_includes_previous_pack_counts(env, capsys):
    out, args, _, tmp = env
    write_pack({"version": "old", "boards": [], "slugs": [
        {"ats": "lever", "slug": f"s{i}", "company": None, "region": "us", "postings": 1}
        for i in range(10)]}, out)
    B.main(args)
    report = json.loads((tmp / "report.json").read_text())
    assert report["previous"] == {"slugs": 10, "boards": 0}
    assert "previous pack: 10 slugs + 0 boards" in capsys.readouterr().out


def test_unreachable_crawl_list_exits_2(env, monkeypatch):
    _, args, _, _ = env

    async def down(client, n, **kw):
        raise B.CrawlFetchError("collinfo: HTTP 504")

    monkeypatch.setattr(B, "list_crawls", down)
    assert B.main(args) == 2


def test_supplement_failure_exits_2_and_writes_nothing(env, monkeypatch):
    import httpx
    out, args, _, _ = env

    async def down(client, **kw):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(B, "supplement_companies", down)
    assert B.main(args) == 2
    assert not out.exists()


def test_corrupt_crawl_cache_is_a_cache_miss(env):
    _, args, crawl_calls, tmp = env
    assert B.main(args) == 0
    (tmp / "cache" / "crawls" / "CC-1.json").write_text("not json")
    assert B.main(args) == 0
    assert crawl_calls == ["CC-1", "CC-1"]


def test_no_lever_skips_the_supplement(env, monkeypatch):
    _, args, _, _ = env

    async def boom(client, **kw):
        raise AssertionError("supplement must not be called")

    monkeypatch.setattr(B, "supplement_companies", boom)
    assert B.main(args + ["--no-lever"]) == 0


def test_lever_supplement_candidates_reach_the_verifier(env, monkeypatch):
    out, args, _, _ = env

    async def fake_supplement(client, **kw):
        return [("Acme Lever", None)]

    monkeypatch.setattr(B, "supplement_companies", fake_supplement)
    FakeVerifier.script["lever:acme-lever"] = _live("lever:acme-lever")
    assert B.main(args) == 0
    assert "lever:acme-lever" in {s.connector_name for s in load_pack(out).slugs}
