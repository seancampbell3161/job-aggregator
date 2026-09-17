from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from src.discovery import DiscoveryConfig, _PROBES_PER_CANDIDATE, _SUPPORTED_ATS, _probe_one_ats, run_discovery
from src.sqlite_db import connect
from src.state_sqlite import SqliteDiscoveredSlugsStore
from src.yc_oss import YcCompany


@pytest.fixture
def discovered_store():
    yield SqliteDiscoveredSlugsStore(connect(":memory:"))


def _yc(name: str, *, slug: str = "", team_size: int = 50) -> YcCompany:
    return YcCompany(
        name=name,
        slug=slug or name.lower(),
        status="Active",
        team_size=team_size,
        website=None,
    )


# ---------- probe-and-classify phase ----------


@pytest.mark.asyncio
async def test_run_discovery_probes_all_6_ats_and_picks_winner(discovered_store):
    """For one candidate, all 6 ATSs are probed concurrently; the highest-
    posting-count match is upserted as ok."""
    yc = AsyncMock()
    yc.fetch = AsyncMock(return_value=[_yc("Acme")])
    cfg = DiscoveryConfig(
        max_validations_per_run=100,
        quarantine_after_failures=5,
        manual_companies=[],
        revalidate_after_days=7,
        no_match_revalidate_after_days=90,
        revalidate_reserve=0,  # isolate this test from the reval-reserve carve-out
    )

    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs").respond(
                200, json={"jobs": [{"id": 1}, {"id": 2}]}
            )
            respx.get("https://api.lever.co/v0/postings/acme").respond(
                200, json=[{"id": 1}, {"id": 2}, {"id": 3}, {"id": 4}, {"id": 5}, {"id": 6}, {"id": 7}]
            )
            respx.get("https://api.ashbyhq.com/posting-api/job-board/acme").respond(404)
            respx.post("https://apply.workable.com/api/v3/accounts/acme/jobs").respond(404)
            respx.get("https://api.smartrecruiters.com/v1/companies/acme/postings").respond(404)
            respx.get(url__regex=r"^https://api\.rippling\.com/").respond(404)
            respx.get(url__regex=r"https://.*\.recruitee\.com/api/offers/").respond(404)
            respx.get(url__regex=r"https://.*\.jobs\.personio\.de/xml").respond(404)
            respx.get(url__regex=r"https://.*\.teamtailor\.com/jobs\.rss").respond(404)

            await run_discovery(
                client=client,
                yc_oss=yc,
                active_set=set(),
                store=discovered_store,
                cfg=cfg,
            )

    row = discovered_store.get("lever:acme")
    assert row is not None
    assert row.validation_status == "ok"
    assert row.company_name == "Acme"
    assert row.last_posting_count == 7
    # Greenhouse had only 2 postings — Lever wins, no greenhouse:acme row.
    assert discovered_store.get("greenhouse:acme") is None


@pytest.mark.asyncio
async def test_run_discovery_records_no_match_when_all_6_fail(discovered_store):
    yc = AsyncMock()
    yc.fetch = AsyncMock(return_value=[_yc("Nomatch")])
    cfg = DiscoveryConfig(
        max_validations_per_run=100, quarantine_after_failures=5,
        manual_companies=[], revalidate_after_days=7,
        no_match_revalidate_after_days=90,
        revalidate_reserve=0,  # isolate this test from the reval-reserve carve-out
    )

    async with httpx.AsyncClient() as client:
        with respx.mock:
            for url in [
                "https://boards-api.greenhouse.io/v1/boards/nomatch/jobs",
                "https://api.lever.co/v0/postings/nomatch",
                "https://api.ashbyhq.com/posting-api/job-board/nomatch",
                "https://api.smartrecruiters.com/v1/companies/nomatch/postings",
                "https://api.rippling.com/platform/api/ats/v1/board/nomatch/jobs",
            ]:
                respx.get(url).respond(404)
            respx.post("https://apply.workable.com/api/v3/accounts/nomatch/jobs").respond(404)
            respx.get(url__regex=r"https://.*\.recruitee\.com/api/offers/").respond(404)
            respx.get(url__regex=r"https://.*\.jobs\.personio\.de/xml").respond(404)
            respx.get(url__regex=r"https://.*\.teamtailor\.com/jobs\.rss").respond(404)

            await run_discovery(
                client=client, yc_oss=yc, active_set=set(),
                store=discovered_store, cfg=cfg,
            )

    row = discovered_store.get("nomatch:nomatch")
    assert row is not None
    assert row.validation_status == "no_match"


@pytest.mark.asyncio
async def test_run_discovery_skips_active_set_candidates(discovered_store):
    """If a candidate's slug is already in the active_set across all 6 ATSs, no probes happen."""
    yc = AsyncMock()
    yc.fetch = AsyncMock(return_value=[_yc("Stripe")])
    cfg = DiscoveryConfig(
        max_validations_per_run=100, quarantine_after_failures=5,
        manual_companies=[], revalidate_after_days=7,
        no_match_revalidate_after_days=90,
        revalidate_reserve=0,  # isolate this test from the reval-reserve carve-out
    )

    async with httpx.AsyncClient() as client:
        with respx.mock:
            # No respx routes registered: any HTTP request would raise.
            await run_discovery(
                client=client, yc_oss=yc,
                active_set={("greenhouse", "stripe"), ("lever", "stripe"),
                            ("ashby", "stripe"), ("workable", "stripe"),
                            ("smartrecruiters", "stripe"), ("rippling", "stripe")},
                store=discovered_store, cfg=cfg,
            )

    assert discovered_store.list_all() == []


@pytest.mark.asyncio
async def test_run_discovery_skips_recent_no_match_candidates(discovered_store):
    """Slugs with a recent no_match row are not re-probed."""
    discovered_store.upsert_no_match("nomatch")
    yc = AsyncMock()
    yc.fetch = AsyncMock(return_value=[_yc("Nomatch")])
    cfg = DiscoveryConfig(
        max_validations_per_run=100, quarantine_after_failures=5,
        manual_companies=[], revalidate_after_days=7,
        no_match_revalidate_after_days=90,
        revalidate_reserve=0,  # isolate this test from the reval-reserve carve-out
    )

    async with httpx.AsyncClient() as client:
        with respx.mock:
            await run_discovery(
                client=client, yc_oss=yc, active_set=set(),
                store=discovered_store, cfg=cfg,
            )

    # Still only the original no_match row (no new probes).
    assert {r.connector_name for r in discovered_store.list_all()} == {"nomatch:nomatch"}


@pytest.mark.asyncio
async def test_run_discovery_includes_manual_companies(discovered_store):
    """manual_companies entries flow through the same pipeline as yc-oss."""
    yc = AsyncMock()
    yc.fetch = AsyncMock(return_value=[])
    cfg = DiscoveryConfig(
        max_validations_per_run=100, quarantine_after_failures=5,
        manual_companies=["anthropic"],
        revalidate_after_days=7, no_match_revalidate_after_days=90,
        revalidate_reserve=0,  # isolate this test from the reval-reserve carve-out
    )

    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get("https://boards-api.greenhouse.io/v1/boards/anthropic/jobs").respond(
                200, json={"jobs": [{"id": 1}]}
            )
            for url in [
                "https://api.lever.co/v0/postings/anthropic",
                "https://api.ashbyhq.com/posting-api/job-board/anthropic",
                "https://api.smartrecruiters.com/v1/companies/anthropic/postings",
            ]:
                respx.get(url).respond(404)
            respx.post("https://apply.workable.com/api/v3/accounts/anthropic/jobs").respond(404)

            await run_discovery(
                client=client, yc_oss=yc, active_set=set(),
                store=discovered_store, cfg=cfg,
            )

    row = discovered_store.get("greenhouse:anthropic")
    assert row is not None
    assert row.validation_status == "ok"


@pytest.mark.asyncio
async def test_run_discovery_dedupes_by_slug_across_sources(discovered_store):
    """If yc-oss and manual_companies produce the same slug, probe once."""
    yc = AsyncMock()
    yc.fetch = AsyncMock(return_value=[_yc("Anthropic")])  # slugifies to "anthropic"
    cfg = DiscoveryConfig(
        max_validations_per_run=100, quarantine_after_failures=5,
        manual_companies=["anthropic"],
        revalidate_after_days=7, no_match_revalidate_after_days=90,
        revalidate_reserve=0,  # isolate this test from the reval-reserve carve-out
    )

    async with httpx.AsyncClient() as client:
        with respx.mock:
            gh = respx.get("https://boards-api.greenhouse.io/v1/boards/anthropic/jobs").respond(
                200, json={"jobs": [{"id": 1}]}
            )
            for url in [
                "https://api.lever.co/v0/postings/anthropic",
                "https://api.ashbyhq.com/posting-api/job-board/anthropic",
                "https://api.smartrecruiters.com/v1/companies/anthropic/postings",
            ]:
                respx.get(url).respond(404)
            respx.post("https://apply.workable.com/api/v3/accounts/anthropic/jobs").respond(404)

            await run_discovery(
                client=client, yc_oss=yc, active_set=set(),
                store=discovered_store, cfg=cfg,
            )

            # Greenhouse should be hit exactly once, not twice.
            assert gh.call_count == 1


@pytest.mark.asyncio
async def test_run_discovery_caps_at_max_validations_per_run(discovered_store):
    """Probe budget is in HTTP probes, not candidates. With 9 probes/candidate,
    a budget of 18 means 2 candidates."""
    candidates = [_yc(f"Co{i}") for i in range(5)]
    yc = AsyncMock()
    yc.fetch = AsyncMock(return_value=candidates)
    cfg = DiscoveryConfig(
        max_validations_per_run=18,    # = 2 candidates × 9 probes/candidate
        quarantine_after_failures=5,
        manual_companies=[],
        revalidate_after_days=7,
        no_match_revalidate_after_days=90,
        revalidate_reserve=0,  # isolate this test from the reval-reserve carve-out
    )

    async with httpx.AsyncClient() as client:
        with respx.mock:
            # All supported ATS families return 404 for any slug; we expect no_match writes.
            respx.get(url__regex=r"https://boards-api\.greenhouse\.io/.*").respond(404)
            respx.get(url__regex=r"https://api\.lever\.co/.*").respond(404)
            respx.get(url__regex=r"https://api\.ashbyhq\.com/.*").respond(404)
            respx.get(url__regex=r"https://api\.smartrecruiters\.com/.*").respond(404)
            respx.post(url__regex=r"https://apply\.workable\.com/.*").respond(404)
            respx.get(url__regex=r"^https://api\.rippling\.com/").respond(404)
            respx.get(url__regex=r"https://.*\.recruitee\.com/api/offers/").respond(404)
            respx.get(url__regex=r"https://.*\.jobs\.personio\.de/xml").respond(404)
            respx.get(url__regex=r"https://.*\.teamtailor\.com/jobs\.rss").respond(404)

            await run_discovery(
                client=client, yc_oss=yc, active_set=set(),
                store=discovered_store, cfg=cfg,
            )

    # Only 2 candidates (out of 5) were probed → 2 no_match rows.
    rows = [r for r in discovered_store.list_all() if r.validation_status == "no_match"]
    assert len(rows) == 2


# ---------- revalidation phase ----------


@pytest.mark.asyncio
async def test_run_discovery_revalidates_stale_ok_rows(discovered_store):
    """After candidate-probing, run revalidation on stale ok rows."""
    old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    discovered_store._put({
        "connector_name": "greenhouse:stale",
        "ats_family": "greenhouse",
        "slug": "stale",
        "company_name": "Stale Co",
        "discovered_at": old,
        "last_validated_at": old,
        "validation_status": "ok",
        "consecutive_failures": 0,
        "last_posting_count": 1,
    })

    yc = AsyncMock()
    yc.fetch = AsyncMock(return_value=[])
    cfg = DiscoveryConfig(
        max_validations_per_run=10, quarantine_after_failures=5,
        manual_companies=[],
        revalidate_after_days=7, no_match_revalidate_after_days=90,
    )

    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get("https://boards-api.greenhouse.io/v1/boards/stale/jobs").respond(
                200, json={"jobs": [{"id": 1}, {"id": 2}, {"id": 3}]}
            )

            await run_discovery(
                client=client, yc_oss=yc, active_set=set(),
                store=discovered_store, cfg=cfg,
            )

    row = discovered_store.get("greenhouse:stale")
    assert row.validation_status == "ok"
    assert row.last_posting_count == 3
    # last_validated_at refreshed to now-ish (not equal to the stale value).
    assert row.last_validated_at != old


@pytest.mark.asyncio
async def test_revalidation_quarantines_after_threshold_failures(discovered_store):
    """A stale ok row that 404s on revalidation increments consecutive_failures."""
    old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    discovered_store._put({
        "connector_name": "greenhouse:dying",
        "ats_family": "greenhouse",
        "slug": "dying",
        "company_name": "Dying Co",
        "discovered_at": old,
        "last_validated_at": old,
        "validation_status": "ok",
        "consecutive_failures": 4,
        "last_posting_count": 1,
    })

    yc = AsyncMock()
    yc.fetch = AsyncMock(return_value=[])
    cfg = DiscoveryConfig(
        max_validations_per_run=10, quarantine_after_failures=5,
        manual_companies=[],
        revalidate_after_days=7, no_match_revalidate_after_days=90,
    )

    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get("https://boards-api.greenhouse.io/v1/boards/dying/jobs").respond(404)

            await run_discovery(
                client=client, yc_oss=yc, active_set=set(),
                store=discovered_store, cfg=cfg,
            )

    row = discovered_store.get("greenhouse:dying")
    assert row.validation_status == "quarantined"
    assert row.consecutive_failures == 5


@pytest.mark.asyncio
async def test_revalidation_skipped_when_budget_consumed(discovered_store):
    """If candidate-probing fully consumes the budget, revalidation is skipped."""
    old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    discovered_store._put({
        "connector_name": "greenhouse:stale-but-skipped",
        "ats_family": "greenhouse",
        "slug": "stale-but-skipped",
        "company_name": "Skip Co",
        "discovered_at": old,
        "last_validated_at": old,
        "validation_status": "ok",
        "consecutive_failures": 0,
        "last_posting_count": 1,
    })

    yc = AsyncMock()
    yc.fetch = AsyncMock(return_value=[_yc("Acme")])  # 1 candidate × 9 probes = 9
    cfg = DiscoveryConfig(
        max_validations_per_run=9,    # exactly 1 candidate's worth, no room for reval
        quarantine_after_failures=5,
        manual_companies=[],
        revalidate_after_days=7, no_match_revalidate_after_days=90,
        revalidate_reserve=0,  # no reserve configured — the crawl legitimately eats the whole budget
    )

    async with httpx.AsyncClient() as client:
        with respx.mock:
            for url in [
                "https://boards-api.greenhouse.io/v1/boards/acme/jobs",
                "https://api.lever.co/v0/postings/acme",
                "https://api.ashbyhq.com/posting-api/job-board/acme",
                "https://api.smartrecruiters.com/v1/companies/acme/postings",
            ]:
                respx.get(url).respond(404)
            respx.post("https://apply.workable.com/api/v3/accounts/acme/jobs").respond(404)
            respx.get(url__regex=r"^https://api\.rippling\.com/").respond(404)
            respx.get(url__regex=r"https://.*\.recruitee\.com/api/offers/").respond(404)
            respx.get(url__regex=r"https://.*\.jobs\.personio\.de/xml").respond(404)
            respx.get(url__regex=r"https://.*\.teamtailor\.com/jobs\.rss").respond(404)
            # NOTE: no respx route for the stale row's URL — would raise if hit.

            await run_discovery(
                client=client, yc_oss=yc, active_set=set(),
                store=discovered_store, cfg=cfg,
            )

    # The stale row's last_validated_at should be UNCHANGED (not revalidated).
    row = discovered_store.get("greenhouse:stale-but-skipped")
    assert row.last_validated_at == old


@pytest.mark.asyncio
async def test_run_discovery_treats_zero_postings_as_no_match(discovered_store):
    """SmartRecruiters returns 200 + empty content for unknown slugs; treating
    that as ok produces ~99% false-positive rate against arbitrary yc-oss
    company names. Require count > 0 to count as a real match."""
    yc = AsyncMock()
    yc.fetch = AsyncMock(return_value=[_yc("Phantom")])
    cfg = DiscoveryConfig(
        max_validations_per_run=100, quarantine_after_failures=5,
        manual_companies=[], revalidate_after_days=7,
        no_match_revalidate_after_days=90,
        revalidate_reserve=0,  # isolate this test from the reval-reserve carve-out
    )

    async with httpx.AsyncClient() as client:
        with respx.mock:
            for url in [
                "https://boards-api.greenhouse.io/v1/boards/phantom/jobs",
                "https://api.lever.co/v0/postings/phantom",
                "https://api.ashbyhq.com/posting-api/job-board/phantom",
            ]:
                respx.get(url).respond(404)
            respx.post("https://apply.workable.com/api/v3/accounts/phantom/jobs").respond(404)
            # SmartRecruiters returns 200 + empty content for unknown slugs.
            respx.get("https://api.smartrecruiters.com/v1/companies/phantom/postings").respond(
                200, json={"offset": 0, "limit": 100, "totalFound": 0, "content": []}
            )
            await run_discovery(
                client=client, yc_oss=yc, active_set=set(),
                store=discovered_store, cfg=cfg,
            )

    # SmartRecruiters' 200+empty does NOT win — falls through to no_match.
    assert discovered_store.get("smartrecruiters:phantom") is None
    row = discovered_store.get("nomatch:phantom")
    assert row is not None
    assert row.validation_status == "no_match"


@pytest.mark.asyncio
async def test_run_discovery_skips_candidate_when_one_ats_is_in_active_set(discovered_store):
    """A slug already polled on ANY one ATS via config is skipped — no need to
    probe the other 4. Fixes the bandwidth-waste issue where 42 known slugs
    cost 4 wasted probes/cycle each."""
    yc = AsyncMock()
    yc.fetch = AsyncMock(return_value=[_yc("Stripe")])
    cfg = DiscoveryConfig(
        max_validations_per_run=100, quarantine_after_failures=5,
        manual_companies=[], revalidate_after_days=7,
        no_match_revalidate_after_days=90,
        revalidate_reserve=0,  # isolate this test from the reval-reserve carve-out
    )

    async with httpx.AsyncClient() as client:
        with respx.mock:
            # No respx routes registered for stripe across any ATS — any HTTP
            # request against any of the 6 ATS endpoints would raise.
            await run_discovery(
                client=client, yc_oss=yc,
                active_set={("greenhouse", "stripe")},  # only one ATS, not all 6
                store=discovered_store, cfg=cfg,
            )

    assert discovered_store.list_all() == []


# ---------- Rippling ATS support ----------


def test_rippling_is_a_supported_ats():
    assert "rippling" in _SUPPORTED_ATS
    assert _PROBES_PER_CANDIDATE == 9


@pytest.mark.asyncio
async def test_probe_rippling_ok_and_404():
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get("https://api.rippling.com/platform/api/ats/v1/board/live/jobs").respond(
                200, json=[{"uuid": "1"}, {"uuid": "2"}, {"uuid": "3"}]
            )
            ok, count = await _probe_one_ats(client=client, ats_family="rippling", slug="live")
            assert ok is True and count == 3

            respx.get("https://api.rippling.com/platform/api/ats/v1/board/dead/jobs").respond(404)
            ok2, count2 = await _probe_one_ats(client=client, ats_family="rippling", slug="dead")
            assert ok2 is False and count2 == 0


from src.fingerprint import FingerprintResult, Seed
from src.state_sqlite import SqliteDiscoveredBoardsStore


def _boards():
    return SqliteDiscoveredBoardsStore(connect(":memory:"))


@pytest.mark.asyncio
async def test_run_board_discovery_upserts_ok_and_nomatch(monkeypatch):
    import src.discovery as disc
    seeds = [Seed(name="3M", domain="3m.com"), Seed(name="Nope", domain="nope.com")]

    async def fake_fp(client, seed):
        if seed.domain == "3m.com":
            return FingerprintResult(name="3M", domain="3m.com", status="matched",
                                     family="workday", identity={"tenant": "3m", "region": "wd1", "site": "Search"})
        return FingerprintResult(name="Nope", domain="nope.com", status="not_found")
    monkeypatch.setattr(disc, "fingerprint_company", fake_fp)

    boards = _boards()
    await disc.run_board_discovery(client=None, seeds=seeds, boards=boards,
                                   max_sweeps_per_run=10, revalidate_after_days=14,
                                   quarantine_threshold=5)
    healthy = boards.list_healthy()
    assert [b.connector_name for b in healthy] == ["workday:3m:Search"]
    assert boards.get("nope.com").status == "not_found"


@pytest.mark.asyncio
async def test_run_board_discovery_respects_budget(monkeypatch):
    import src.discovery as disc
    seeds = [Seed(name=f"C{i}", domain=f"c{i}.com") for i in range(10)]
    calls = []

    async def fake_fp(client, seed):
        calls.append(seed.domain)
        return FingerprintResult(name=seed.name, domain=seed.domain, status="not_found")
    monkeypatch.setattr(disc, "fingerprint_company", fake_fp)

    await disc.run_board_discovery(client=None, seeds=seeds, boards=_boards(),
                                   max_sweeps_per_run=3, revalidate_after_days=14,
                                   quarantine_threshold=5)
    assert len(calls) == 3  # budget respected


@pytest.mark.asyncio
async def test_run_board_discovery_transient_miss_does_not_demote_live_board(monkeypatch):
    """A live ok board must survive a transient re-fingerprint miss — poll-health
    (not the discovery sweep) owns real death via 404/410 tracking."""
    import src.discovery as disc
    boards = _boards()
    old = "2020-01-01T00:00:00+00:00"  # stale, so it's actually re-swept this cycle
    boards._put({
        "domain": "3m.com", "name": "3M", "status": "ok", "family": "workday",
        "identity": {"tenant": "3m", "region": "wd1", "site": "Search"},
        "connector_name": "workday:3m:Search", "company": "3M",
        "last_swept_at": old, "failure_streak": 0,
    })
    seeds = [Seed(name="3M", domain="3m.com")]

    async def fake_fp(client, seed):
        return FingerprintResult(name="3M", domain="3m.com", status="not_found")
    monkeypatch.setattr(disc, "fingerprint_company", fake_fp)

    await disc.run_board_discovery(client=None, seeds=seeds, boards=boards,
                                   max_sweeps_per_run=10, revalidate_after_days=14,
                                   quarantine_threshold=5)
    row = boards.get("3m.com")
    assert row.status == "ok" and row.failure_streak == 0
    assert [b.domain for b in boards.list_healthy()] == ["3m.com"]


@pytest.mark.asyncio
async def test_run_board_discovery_skips_fresh_seeds(monkeypatch):
    import src.discovery as disc
    boards = _boards()
    boards.upsert_ok("3m.com", name="3M", family="workday",
                     identity={"tenant": "3m", "region": "wd1", "site": "Search"},
                     connector_name="workday:3m:Search")  # just swept → fresh
    seeds = [Seed(name="3M", domain="3m.com")]

    async def fake_fp(client, seed):
        raise AssertionError("fresh seed must not be re-fingerprinted")
    monkeypatch.setattr(disc, "fingerprint_company", fake_fp)

    await disc.run_board_discovery(client=None, seeds=seeds, boards=boards,
                                   max_sweeps_per_run=10, revalidate_after_days=14,
                                   quarantine_threshold=5)  # no raise → fresh skipped


# ---------- Task 6: priority-queue budget — reserve + candidate validation ----------

from unittest.mock import patch


class _NoYc:
    async def fetch(self, client):
        return []


def _slug_store():
    return SqliteDiscoveredSlugsStore(connect(":memory:"))


def _cfg(**kw):
    defaults = dict(max_validations_per_run=600, manual_companies=[],
                    yc_oss_enabled=False, revalidate_reserve=0)
    defaults.update(kw)
    return DiscoveryConfig(**defaults)


@pytest.mark.asyncio
async def test_slug_candidate_claimed_family_hit_costs_one_probe():
    store = _slug_store()
    store.upsert_candidate("greenhouse:newco", company_name="NewCo",
                           origin="hiringcafe", claimed_family="greenhouse")
    probe = AsyncMock(return_value=(True, 7))
    with patch("src.discovery._probe_one_ats", new=probe):
        await run_discovery(client=None, yc_oss=_NoYc(), active_set=set(),
                            store=store, cfg=_cfg())
    assert probe.await_count == 1
    row = store.get("greenhouse:newco")
    assert row.validation_status == "ok" and row.last_posting_count == 7


@pytest.mark.asyncio
async def test_slug_candidate_falls_back_to_other_families_and_rekeys():
    store = _slug_store()
    store.upsert_candidate("greenhouse:newco", company_name="NewCo",
                           origin="hiringcafe", claimed_family="greenhouse")

    async def probe(*, client, ats_family, slug):
        return (True, 5) if ats_family == "lever" else (False, 0)

    with patch("src.discovery._probe_one_ats", side_effect=probe):
        await run_discovery(client=None, yc_oss=_NoYc(), active_set=set(),
                            store=store, cfg=_cfg())
    assert store.get("greenhouse:newco") is None          # staged row re-keyed away
    assert store.get("lever:newco").validation_status == "ok"


@pytest.mark.asyncio
async def test_slug_candidate_full_miss_resolves_no_match_in_place():
    store = _slug_store()
    store.upsert_candidate("greenhouse:ghost", claimed_family="greenhouse")
    with patch("src.discovery._probe_one_ats", new=AsyncMock(return_value=(False, 0))):
        await run_discovery(client=None, yc_oss=_NoYc(), active_set=set(),
                            store=store, cfg=_cfg())
    assert store.get("greenhouse:ghost").validation_status == "no_match"
    assert store.is_recent_no_match("ghost", fresh_within_days=90) is True
    assert store.list_candidates() == []


@pytest.mark.asyncio
async def test_board_candidate_promotes_on_verified_postings():
    store = _slug_store()
    boards = SqliteDiscoveredBoardsStore(connect(":memory:"))
    boards.upsert_candidate("acme.wd5.myworkdayjobs.com", name="Acme", family="workday",
                            identity={"tenant": "acme", "region": "wd5", "site": "Ext"},
                            connector_name="workday:acme:Ext", origin="hiringcafe")
    with patch("src.discovery.verify_identity", new=AsyncMock(return_value=12)):
        await run_discovery(client=None, yc_oss=_NoYc(), active_set=set(),
                            store=store, cfg=_cfg(), boards=boards)
    row = boards.get("acme.wd5.myworkdayjobs.com")
    assert row.status == "ok"
    assert row.connector_name == "workday:acme:Ext"


@pytest.mark.asyncio
async def test_board_candidate_failure_streaks():
    store = _slug_store()
    boards = SqliteDiscoveredBoardsStore(connect(":memory:"))
    boards.upsert_candidate("x.wd1.myworkdayjobs.com", name="X", family="workday",
                            identity={"tenant": "x", "region": "wd1", "site": "S"},
                            connector_name="workday:x:S")
    with patch("src.discovery.verify_identity", new=AsyncMock(side_effect=RuntimeError("net"))):
        await run_discovery(client=None, yc_oss=_NoYc(), active_set=set(),
                            store=store, cfg=_cfg(), boards=boards)
    row = boards.get("x.wd1.myworkdayjobs.com")
    assert row.status == "candidate" and row.failure_streak == 1


@pytest.mark.asyncio
async def test_revalidate_reserve_survives_candidate_exhaustion():
    """With a tiny budget and a reserve, revalidation still runs."""
    store = _slug_store()
    # a stale ok row that needs revalidation
    store.upsert_ok("greenhouse:stale", company_name="Stale")
    import json
    row = store.get("greenhouse:stale")
    store._conn.execute(
        "UPDATE discovered_slugs SET data = ? WHERE connector_name = ?",
        (json.dumps({**{k: v for k, v in row.__dict__.items() if v is not None},
                     "last_validated_at": "2000-01-01T00:00:00+00:00"}),
         "greenhouse:stale"),
    )
    # enough candidates to eat the whole non-reserved budget
    for i in range(10):
        store.upsert_candidate(f"greenhouse:c{i}", claimed_family="greenhouse")
    probe = AsyncMock(return_value=(True, 1))
    with patch("src.discovery._probe_one_ats", new=probe):
        await run_discovery(client=None, yc_oss=_NoYc(), active_set=set(),
                            store=store, cfg=_cfg(max_validations_per_run=8,
                                                  revalidate_reserve=2))
    # stale row was revalidated (its last_validated_at moved off 2000-01-01)
    assert store.get("greenhouse:stale").last_validated_at > "2001"


# ---------- conversion chain (spec part 2) ----------

from src.discovery import _run_candidate_chain
# NOTE: FingerprintResult is already imported at tests/test_discovery.py:495 —
# do not re-import it.


def _fp(status, *, family=None, identity=None, posting_count=0):
    return FingerprintResult(name="X", domain="x.com", status=status,
                             family=family, identity=identity,
                             posting_count=posting_count)


@pytest.mark.asyncio
async def test_chain_first_variant_hit_costs_one_candidate_charge():
    store = _slug_store()
    probe = AsyncMock(return_value=("greenhouse", 9))
    with patch("src.discovery._probe_all_ats", new=probe):
        budget, outcome = await _run_candidate_chain(
            client=None, name="Acme", website=None, alt_slug=None,
            store=store, boards=None, active_set=set(), budget=100)
    assert (budget, outcome) == (91, "ok")
    assert probe.await_count == 1
    row = store.get("greenhouse:acme")
    assert row.validation_status == "ok" and row.last_posting_count == 9


@pytest.mark.asyncio
async def test_chain_second_variant_hit_costs_two_candidate_charges():
    store = _slug_store()
    probe = AsyncMock(side_effect=[None, ("lever", 3)])
    with patch("src.discovery._probe_all_ats", new=probe):
        budget, outcome = await _run_candidate_chain(
            client=None, name="Acme Inc", website="https://www.getacme.io",
            alt_slug=None, store=store, boards=None, active_set=set(), budget=100)
    assert (budget, outcome) == (82, "ok")
    # variant 1 "acme" missed; variant 2 (domain) "getacme" hit
    assert store.get("lever:getacme").validation_status == "ok"
    assert store.get("nomatch:acme") is None  # a hit persists NO no_match row


@pytest.mark.asyncio
async def test_chain_fingerprint_slug_family_routes_to_slug_store():
    store = _slug_store()
    fp = AsyncMock(return_value=_fp("matched", family="lever",
                                    identity={"slug": "acme-hq"}, posting_count=4))
    with patch("src.discovery._probe_all_ats", new=AsyncMock(return_value=None)), \
         patch("src.discovery.fingerprint_company", new=fp):
        budget, outcome = await _run_candidate_chain(
            client=None, name="Acme", website="https://acme.com", alt_slug=None,
            store=store, boards=None, active_set=set(), budget=100)
    # 9 (variant "acme"; domain dedups) + 9 (fingerprint, charged flat) = 18
    assert (budget, outcome) == (82, "ok")
    assert store.get("lever:acme-hq").last_posting_count == 4
    fp.assert_awaited_once()
    seed = fp.await_args.args[1]
    assert seed.name == "Acme" and seed.domain == "acme.com"


@pytest.mark.asyncio
async def test_chain_fingerprint_structured_family_routes_to_boards():
    store = _slug_store()
    boards = SqliteDiscoveredBoardsStore(connect(":memory:"))
    identity = {"tenant": "acme", "region": "wd5", "site": "Ext"}
    fp = AsyncMock(return_value=_fp("matched", family="workday",
                                    identity=identity, posting_count=12))
    with patch("src.discovery._probe_all_ats", new=AsyncMock(return_value=None)), \
         patch("src.discovery.fingerprint_company", new=fp):
        budget, outcome = await _run_candidate_chain(
            client=None, name="Acme", website="https://www.acme.com", alt_slug=None,
            store=store, boards=boards, active_set=set(), budget=100)
    assert (budget, outcome) == (82, "board_ok")
    row = boards.get("acme.com")  # keyed by seed domain, like the enterprise sweep
    assert row.status == "ok" and row.family == "workday"
    assert row.connector_name == "workday:acme:Ext"
    assert store.list_no_match() == []


@pytest.mark.asyncio
async def test_chain_definitive_fingerprint_miss_writes_exhausted_no_match():
    from src.state import no_match_exhausted
    store = _slug_store()
    with patch("src.discovery._probe_all_ats", new=AsyncMock(return_value=None)), \
         patch("src.discovery.fingerprint_company",
               new=AsyncMock(return_value=_fp("not_found"))):
        budget, outcome = await _run_candidate_chain(
            client=None, name="Ghost Co", website="https://ghost.dev", alt_slug=None,
            store=store, boards=None, active_set=set(), budget=100)
    assert outcome == "no_match"
    row = store.get("nomatch:ghost-co")  # keyed by FIRST variant
    assert row.company_name == "Ghost Co" and row.website == "https://ghost.dev"
    assert row.methods_tried == ["slug:ghost-co", "domain:ghost", "fingerprint"]
    assert no_match_exhausted(row) is True


@pytest.mark.asyncio
async def test_chain_fingerprint_error_stays_non_exhausted():
    from src.state import no_match_exhausted
    store = _slug_store()
    with patch("src.discovery._probe_all_ats", new=AsyncMock(return_value=None)), \
         patch("src.discovery.fingerprint_company",
               new=AsyncMock(return_value=_fp("error"))):
        _, outcome = await _run_candidate_chain(
            client=None, name="Flaky", website="https://flaky.dev", alt_slug=None,
            store=store, boards=None, active_set=set(), budget=100)
    assert outcome == "no_match"
    row = store.get("nomatch:flaky")
    # domain variant "flaky" dedups against the name-slug → one variant tried;
    # "fingerprint" NOT appended on a transient error.
    assert row.methods_tried == ["slug:flaky"]
    assert no_match_exhausted(row) is False


@pytest.mark.asyncio
async def test_chain_budget_cutoff_persists_nothing():
    store = _slug_store()
    probe = AsyncMock(return_value=None)
    with patch("src.discovery._probe_all_ats", new=probe):
        # budget 11: variant 1 costs 9, variant 2 needs 9 more → cut off
        budget, outcome = await _run_candidate_chain(
            client=None, name="Acme Inc", website="https://www.getacme.io",
            alt_slug=None, store=store, boards=None, active_set=set(), budget=11)
    assert (budget, outcome) == (2, "budget")
    assert probe.await_count == 1
    assert store.list_all() == []  # nothing persisted — idempotent re-run next cycle


@pytest.mark.asyncio
async def test_chain_skips_company_already_active_under_any_variant():
    store = _slug_store()
    probe = AsyncMock()
    with patch("src.discovery._probe_all_ats", new=probe):
        budget, outcome = await _run_candidate_chain(
            client=None, name="Acme Inc", website="https://www.getacme.io",
            alt_slug=None, store=store, boards=None,
            active_set={("lever", "getacme")},  # active under the DOMAIN variant
            budget=100)
    assert (budget, outcome) == (100, "active")
    probe.assert_not_awaited()


@pytest.mark.asyncio
async def test_chain_structured_match_without_boards_store_writes_exhausted_no_match():
    """Plan-mandated tradeoff: a structured-family fingerprint hit with no
    boards store to persist it is written off as an exhausted no_match —
    the alternative (retrying forever for a board we can never store) was
    rejected in the plan. This test locks in the disclosed behavior."""
    from src.state import no_match_exhausted
    store = _slug_store()
    fp = AsyncMock(return_value=_fp("matched", family="workday",
                                    identity={"tenant": "acme", "region": "wd5", "site": "Ext"},
                                    posting_count=12))
    with patch("src.discovery._probe_all_ats", new=AsyncMock(return_value=None)), \
         patch("src.discovery.fingerprint_company", new=fp):
        budget, outcome = await _run_candidate_chain(
            client=None, name="Acme", website="https://acme.com", alt_slug=None,
            store=store, boards=None, active_set=set(), budget=100)
    assert (budget, outcome) == (82, "no_match")
    row = store.get("nomatch:acme")
    assert row.methods_tried == ["slug:acme", "fingerprint"]
    assert no_match_exhausted(row) is True


@pytest.mark.asyncio
async def test_chain_skipped_when_no_variants_derivable():
    store = _slug_store()
    probe = AsyncMock()
    with patch("src.discovery._probe_all_ats", new=probe):
        budget, outcome = await _run_candidate_chain(
            client=None, name="", website=None, alt_slug=None,
            store=store, boards=None, active_set=set(), budget=100)
    assert (budget, outcome) == (100, "skipped")
    probe.assert_not_awaited()
    assert store.list_all() == []


# ---------- chain wiring: crawl + exhausted drain (spec part 2) ----------

import json as _json


def _backdate_no_match(store, slug, iso):
    """Rewrite a sqlite no_match row's last_validated_at (test-only)."""
    row = store._conn.execute(
        "SELECT data FROM discovered_slugs WHERE connector_name = ?",
        (f"nomatch:{slug}",),
    ).fetchone()
    data = _json.loads(row["data"])
    data["last_validated_at"] = iso
    store._conn.execute(
        "UPDATE discovered_slugs SET data = ? WHERE connector_name = ?",
        (_json.dumps(data), f"nomatch:{slug}"),
    )


class _OneYc:
    """yc_oss stub returning one fixed company."""
    def __init__(self, company):
        self._c = company
    async def fetch(self, client):
        return [self._c]


@pytest.mark.asyncio
async def test_crawl_no_match_records_chain_metadata():
    store = _slug_store()
    yc = _OneYc(YcCompany(name="Ghost Co", slug="ghost-yc", status="Active",
                          team_size=50, website="https://ghost.dev"))
    with patch("src.discovery._probe_all_ats", new=AsyncMock(return_value=None)), \
         patch("src.discovery.fingerprint_company",
               new=AsyncMock(return_value=FingerprintResult(
                   name="Ghost Co", domain="ghost.dev", status="not_found"))):
        await run_discovery(client=None, yc_oss=yc, active_set=set(),
                            store=store, cfg=_cfg(yc_oss_enabled=True))
    row = store.get("nomatch:ghost-co")
    assert row is not None
    assert row.website == "https://ghost.dev"
    assert row.methods_tried == [
        "slug:ghost-co", "domain:ghost", "alt:ghost-yc", "fingerprint",
    ]


@pytest.mark.asyncio
async def test_crawl_skips_expired_but_exhausted_no_match():
    """An expired EXHAUSTED row must NOT retry in the crawl phase — it drains
    in Phase 4 only (lowest priority)."""
    store = _slug_store()
    store.upsert_no_match("ghost-co", company_name="Ghost Co",
                          website="https://ghost.dev",
                          methods_tried=["slug:ghost-co", "domain:ghost",
                                         "alt:ghost-yc", "fingerprint"])
    _backdate_no_match(store, "ghost-co", "2020-01-01T00:00:00+00:00")
    yc = _OneYc(YcCompany(name="Ghost Co", slug="ghost-yc", status="Active",
                          team_size=50, website="https://ghost.dev"))
    probe = AsyncMock(return_value=None)
    # No stale ok rows → revalidation is a no-op → Phase 4 drains the row.
    fp = AsyncMock(return_value=FingerprintResult(
        name="Ghost Co", domain="ghost.dev", status="not_found"))
    with patch("src.discovery._probe_all_ats", new=probe), \
         patch("src.discovery.fingerprint_company", new=fp):
        await run_discovery(client=None, yc_oss=yc, active_set=set(),
                            store=store, cfg=_cfg(yc_oss_enabled=True))
    # Crawl skipped it (exhausted), Phase 4 drained it: exactly ONE chain run.
    # Phase 4 passes alt_slug=None (not stored), so the chain has only the
    # name-slug + domain variants: 2 probes + 1 fingerprint.
    assert probe.await_count == 2
    assert fp.await_count == 1
    # The drain refreshed the row's timer.
    assert store.get("nomatch:ghost-co").last_validated_at > "2025"


@pytest.mark.asyncio
async def test_expired_non_exhausted_no_match_retries_in_crawl():
    """Legacy rows (no methods_tried) re-enter at crawl priority and run the
    full chain with the feed's name/website."""
    store = _slug_store()
    store.upsert_no_match("ghost-co")  # legacy plain row
    _backdate_no_match(store, "ghost-co", "2020-01-01T00:00:00+00:00")
    yc = _OneYc(YcCompany(name="Ghost Co", slug="ghost-yc", status="Active",
                          team_size=50, website="https://ghost.dev"))
    probe = AsyncMock(return_value=("lever", 5))
    with patch("src.discovery._probe_all_ats", new=probe):
        await run_discovery(client=None, yc_oss=yc, active_set=set(),
                            store=store, cfg=_cfg(yc_oss_enabled=True))
    assert store.get("lever:ghost-co").validation_status == "ok"


@pytest.mark.asyncio
async def test_exhausted_drain_only_after_revalidation_demand_met():
    """With budget only large enough for revalidation, the exhausted row
    must NOT be probed."""
    store = _slug_store()
    # one stale ok row that will consume the tiny reval budget
    store.upsert_ok("greenhouse:stale", company_name="Stale")
    row = store._conn.execute(
        "SELECT data FROM discovered_slugs WHERE connector_name = ?",
        ("greenhouse:stale",)).fetchone()
    data = _json.loads(row["data"])
    data["last_validated_at"] = "2000-01-01T00:00:00+00:00"
    store._conn.execute(
        "UPDATE discovered_slugs SET data = ? WHERE connector_name = ?",
        (_json.dumps(data), "greenhouse:stale"))
    # one expired exhausted no_match
    store.upsert_no_match("ghost", company_name="Ghost",
                          methods_tried=["slug:ghost"])
    _backdate_no_match(store, "ghost", "2020-01-01T00:00:00+00:00")

    probe_one = AsyncMock(return_value=(True, 1))   # revalidation path
    probe_all = AsyncMock(return_value=None)        # chain path
    with patch("src.discovery._probe_one_ats", new=probe_one), \
         patch("src.discovery._probe_all_ats", new=probe_all):
        await run_discovery(client=None, yc_oss=_NoYc(), active_set=set(),
                            store=store,
                            cfg=_cfg(max_validations_per_run=1,
                                     revalidate_reserve=1))
    probe_one.assert_awaited_once()   # revalidation happened
    probe_all.assert_not_awaited()    # drain did NOT (leftover 0 < 9)


# ---------- final whole-branch review fixes (2026-07-10) ----------


@pytest.mark.asyncio
async def test_manual_entry_no_match_suppresses_next_run():
    """Final-review Important #1: a manual entry whose normalization alters
    the slug must key its no_match under the chain primary, so the next
    run's suppression lookup hits instead of re-probing the chain forever."""
    store = _slug_store()
    probe = AsyncMock(return_value=None)
    cfg = _cfg(manual_companies=["acme-corp"])
    with patch("src.discovery._probe_all_ats", new=probe):
        await run_discovery(client=None, yc_oss=_NoYc(), active_set=set(),
                            store=store, cfg=cfg)
        first_run_probes = probe.await_count
        await run_discovery(client=None, yc_oss=_NoYc(), active_set=set(),
                            store=store, cfg=cfg)
    assert probe.await_count == first_run_probes  # run 2 fully suppressed


@pytest.mark.asyncio
async def test_chain_skips_company_with_ok_board_for_domain():
    """Final-review Important #2: an ok board row for the seed domain counts
    as active — no variant probes, no re-fingerprint."""
    store = _slug_store()
    boards = SqliteDiscoveredBoardsStore(connect(":memory:"))
    boards.upsert_ok("acme.com", name="Acme", family="workday",
                     identity={"tenant": "acme", "region": "wd5", "site": "Ext"},
                     connector_name="workday:acme:Ext", company="Acme")
    probe = AsyncMock()
    fp = AsyncMock()
    with patch("src.discovery._probe_all_ats", new=probe), \
         patch("src.discovery.fingerprint_company", new=fp):
        budget, outcome = await _run_candidate_chain(
            client=None, name="Acme", website="https://www.acme.com", alt_slug=None,
            store=store, boards=boards, active_set=set(), budget=100)
    assert (budget, outcome) == (100, "active")
    probe.assert_not_awaited()
    fp.assert_not_awaited()


@pytest.mark.asyncio
async def test_chain_skips_company_with_quarantined_board_for_domain():
    """Quarantined board domains are skipped too — mirroring the sweep."""
    store = _slug_store()
    boards = SqliteDiscoveredBoardsStore(connect(":memory:"))
    # build a quarantined row via the store's own failure path
    boards.upsert_result("acme.com", name="Acme", status="error",
                         quarantine_threshold=1)
    assert boards.get("acme.com").status == "quarantined"  # fixture sanity
    probe = AsyncMock()
    with patch("src.discovery._probe_all_ats", new=probe):
        budget, outcome = await _run_candidate_chain(
            client=None, name="Acme", website="https://www.acme.com", alt_slug=None,
            store=store, boards=boards, active_set=set(), budget=100)
    assert (budget, outcome) == (100, "active")
    probe.assert_not_awaited()


@pytest.mark.asyncio
async def test_exhausted_drain_active_outcome_refreshes_timer():
    """Final-review Minor fold-in: a drained row resolving 'active' must
    leave the front of the drain queue (timer refreshed, fields preserved)
    and spend nothing."""
    store = _slug_store()
    store.upsert_no_match("ghost", company_name="Ghost", methods_tried=["slug:ghost"])
    _backdate_no_match(store, "ghost", "2020-01-01T00:00:00+00:00")
    probe = AsyncMock()
    with patch("src.discovery._probe_all_ats", new=probe):
        await run_discovery(client=None, yc_oss=_NoYc(),
                            active_set={("lever", "ghost")},
                            store=store, cfg=_cfg())
    probe.assert_not_awaited()
    row = store.get("nomatch:ghost")
    assert row.last_validated_at > "2025"          # timer refreshed
    assert row.methods_tried == ["slug:ghost"]     # learned fields preserved


# ---------- name-based candidate drain (spec part 3: VC staging rows) ----------


@pytest.mark.asyncio
async def test_name_candidate_drains_via_chain_and_rekeys():
    store = _slug_store()
    store.upsert_candidate("candidate:acme", company_name="Acme Inc.",
                           website="acme.com", origin="vc:a16z")

    async def probe(*, client, ats_family, slug):
        return (True, 5) if ats_family == "greenhouse" else (False, 0)

    with patch("src.discovery._probe_one_ats", side_effect=probe) as mock:
        await run_discovery(client=None, yc_oss=_NoYc(), active_set=set(),
                            store=store, cfg=_cfg())
    assert store.get("candidate:acme") is None            # staging row removed
    row = store.get("greenhouse:acme")
    assert row.validation_status == "ok" and row.company_name == "Acme Inc."
    # "Acme Inc." + "acme.com" + alt all collapse to the single variant "acme"
    assert mock.call_count == 9


@pytest.mark.asyncio
async def test_name_candidate_full_miss_writes_nomatch_and_removes_row():
    store = _slug_store()
    store.upsert_candidate("candidate:acme", company_name="Acme Inc.",
                           website="acme.com", origin="vc:a16z")
    with patch("src.discovery._probe_one_ats", new=AsyncMock(return_value=(False, 0))), \
         patch("src.discovery.fingerprint_company",
               new=AsyncMock(return_value=_fp("not_found"))):
        await run_discovery(client=None, yc_oss=_NoYc(), active_set=set(),
                            store=store, cfg=_cfg())
    assert store.get("candidate:acme") is None
    nomatch = store.get("nomatch:acme")
    assert nomatch.methods_tried == ["slug:acme", "fingerprint"]
    assert nomatch.website == "acme.com"
    assert store.is_recent_no_match("acme", fresh_within_days=90) is True


@pytest.mark.asyncio
async def test_name_candidate_board_match_promotes_board_and_removes_row():
    store = _slug_store()
    boards = SqliteDiscoveredBoardsStore(connect(":memory:"))
    store.upsert_candidate("candidate:acme", company_name="Acme Inc.",
                           website="acme.com", origin="vc:a16z")
    with patch("src.discovery._probe_one_ats", new=AsyncMock(return_value=(False, 0))), \
         patch("src.discovery.fingerprint_company",
               new=AsyncMock(return_value=_fp(
                   "matched", family="workday",
                   identity={"tenant": "acme", "region": "wd5", "site": "Ext"},
                   posting_count=3))):
        await run_discovery(client=None, yc_oss=_NoYc(), active_set=set(),
                            store=store, cfg=_cfg(), boards=boards)
    assert store.get("candidate:acme") is None
    brow = boards.get("acme.com")
    assert brow.status == "ok" and brow.family == "workday"


@pytest.mark.asyncio
async def test_name_candidate_budget_cutoff_leaves_row_staged():
    store = _slug_store()
    store.upsert_candidate("candidate:acme", company_name="Acme Inc.",
                           website="acme.com", origin="vc:a16z")
    probe = AsyncMock(return_value=(True, 5))
    with patch("src.discovery._probe_one_ats", new=probe):
        await run_discovery(client=None, yc_oss=_NoYc(), active_set=set(),
                            store=store, cfg=_cfg(max_validations_per_run=5))
    assert probe.await_count == 0                         # 5 < 9: chain never started
    assert store.get("candidate:acme").validation_status == "candidate"
    assert store.get("nomatch:acme") is None              # nothing persisted


@pytest.mark.asyncio
async def test_name_candidate_already_active_deletes_row_zero_probes():
    store = _slug_store()
    store.upsert_candidate("candidate:acme", company_name="Acme Inc.",
                           website="acme.com", origin="vc:a16z")
    probe = AsyncMock(return_value=(True, 5))
    with patch("src.discovery._probe_one_ats", new=probe):
        await run_discovery(client=None, yc_oss=_NoYc(),
                            active_set={("lever", "acme")},
                            store=store, cfg=_cfg())
    assert probe.await_count == 0
    assert store.get("candidate:acme") is None            # moot: already polled
    assert store.get("nomatch:acme") is None


# ---------- EU ATS families: recruitee / personio / teamtailor ----------

PERSONIO_PROBE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<workzag-jobs><position><id>1</id><name>SWE</name></position>
<position><id>2</id><name>SRE</name></position></workzag-jobs>"""

TEAMTAILOR_PROBE_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:tt="https://teamtailor.com/locations"><channel>
<item><title>SWE</title><guid>a</guid></item></channel></rss>"""


@pytest.mark.asyncio
async def test_probe_recruitee_counts_offers():
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get("https://acme.recruitee.com/api/offers/").respond(
                200, json={"offers": [{"id": 1}, {"id": 2}, {"id": 3}]}
            )
            ok, count = await _probe_one_ats(client=client, ats_family="recruitee", slug="acme")
    assert ok is True and count == 3


@pytest.mark.asyncio
async def test_probe_personio_counts_positions_xml():
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get("https://acme.jobs.personio.de/xml").respond(200, text=PERSONIO_PROBE_XML)
            ok, count = await _probe_one_ats(client=client, ats_family="personio", slug="acme")
    assert ok is True and count == 2


@pytest.mark.asyncio
async def test_probe_personio_redirect_is_a_miss():
    # Unknown slugs 307-redirect to the personio.com marketing site: not a tenant.
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get("https://acme.jobs.personio.de/xml").respond(
                307, headers={"location": "https://personio.com"}
            )
            ok, count = await _probe_one_ats(client=client, ats_family="personio", slug="acme")
    assert ok is False and count == 0


@pytest.mark.asyncio
async def test_probe_teamtailor_counts_items_xml():
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get("https://acme.teamtailor.com/jobs.rss").respond(200, text=TEAMTAILOR_PROBE_RSS)
            ok, count = await _probe_one_ats(client=client, ats_family="teamtailor", slug="acme")
    assert ok is True and count == 1


@pytest.mark.asyncio
async def test_probe_xml_empty_feed_is_not_a_hit():
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get("https://acme.jobs.personio.de/xml").respond(
                200, text='<?xml version="1.0"?><workzag-jobs></workzag-jobs>'
            )
            ok, count = await _probe_one_ats(client=client, ats_family="personio", slug="acme")
    assert ok is False and count == 0
