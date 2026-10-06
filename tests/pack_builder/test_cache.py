import json
from datetime import datetime, timedelta, timezone

from scripts.pack_builder.cache import BuildCache, CheckResult

NOW = datetime(2026, 10, 6, tzinfo=timezone.utc)
LIVE = CheckResult("live", postings=12, us_postings=10, eu_postings=0, company="Acme",
                   connector_name="greenhouse:acme", identity={"slug": "acme"})


def test_put_get_roundtrip(tmp_path):
    cache = BuildCache(tmp_path / "c.db")
    cache.put("greenhouse:{\"slug\": \"acme\"}", "greenhouse", LIVE, now=NOW)
    result, at = cache.get("greenhouse:{\"slug\": \"acme\"}")
    assert result == LIVE and at == NOW


def test_needs_check(tmp_path):
    cache = BuildCache(tmp_path / "c.db")
    cache.put("fresh", "greenhouse", LIVE, now=NOW - timedelta(days=2))
    cache.put("stale", "greenhouse", LIVE, now=NOW - timedelta(days=9))
    cache.put("deferred", "greenhouse", CheckResult("deferred"), now=NOW)
    cache.put("dead", "greenhouse", CheckResult("dead"), now=NOW)
    check = lambda k: cache.needs_check(k, max_age_days=7, now=NOW)  # noqa: E731
    assert (check("fresh"), check("stale"), check("deferred"), check("dead"), check("missing")) \
        == (False, True, True, False, True)


def test_results_survive_reopen(tmp_path):
    cache = BuildCache(tmp_path / "c.db")
    cache.put("k", "greenhouse", LIVE, now=NOW)
    # No close(): simulates Ctrl-C. Each put must already be committed.
    again = BuildCache(tmp_path / "c.db")
    assert again.get("k")[0] == LIVE


def test_rows_from_another_cache_schema_still_load(tmp_path):
    """A row written by an older or newer builder may carry fields this
    CheckResult doesn't have (or lack ones it added); unknown ones are ignored."""
    cache = BuildCache(tmp_path / "c.db")
    cache._conn.execute(
        "INSERT INTO checks VALUES (?, ?, ?, ?)",
        ("k", "greenhouse",
         json.dumps({"status": "live", "postings": 4, "company": "Acme", "future_field": 1}),
         NOW.isoformat()))
    expected = CheckResult("live", postings=4, company="Acme")
    assert cache.get("k") == (expected, NOW)
    assert cache.results(["k"]) == {"k": ("greenhouse", expected)}


def test_row_without_reason_loads_with_none(tmp_path):
    cache = BuildCache(tmp_path / "c.db")
    cache._conn.execute(
        "INSERT INTO checks VALUES (?, ?, ?, ?)",
        ("k", "workday", json.dumps({"status": "dead"}), NOW.isoformat()))
    assert cache.get("k")[0].reason is None
    cache.put("k2", "workday", CheckResult("dead", reason="HTTP 400"), now=NOW)
    assert cache.get("k2")[0].reason == "HTTP 400"


def test_results_returns_only_requested_keys(tmp_path):
    cache = BuildCache(tmp_path / "c.db")
    cache.put("a", "greenhouse", LIVE, now=NOW)
    cache.put("b", "workday", CheckResult("dead"), now=NOW)
    assert cache.results(["a", "zzz"]) == {"a": ("greenhouse", LIVE)}


def test_all_results_returns_every_row(tmp_path):
    cache = BuildCache(tmp_path / "c.db")
    dead = CheckResult("dead")
    cache.put("a", "greenhouse", LIVE, now=NOW)
    cache.put("b", "workday", dead, now=NOW)
    assert cache.all_results() == {"a": ("greenhouse", LIVE), "b": ("workday", dead)}
