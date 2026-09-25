"""Pack OFF must not block discovery from finding the same companies.

A starter row hidden by the gate is not "already polled": discovery's own
fresh confirmation (yc-oss / manual chain, candidate chain, board sweep)
reclaims it so it is polled like any discovered board. Revalidation and
failure writes keep the starter tag."""
from unittest.mock import AsyncMock, patch

import pytest

from src.config import AppConfig
from src.discovery import DiscoveryConfig, _run_candidate_chain, run_board_discovery, run_discovery
from src.fingerprint import FingerprintResult, Seed
from src.sqlite_db import connect
from src.starter_pack import ORIGIN_US, gate_stores
from src.state_sqlite import SqliteDiscoveredBoardsStore, SqliteDiscoveredSlugsStore

OFF = AppConfig.model_validate({"discovery": {"starter_pack": False}})
ON = AppConfig.model_validate({"discovery": {"starter_pack": True}})
WD = {"tenant": "acme", "region": "wd5", "site": "Ext"}


def _stores():
    conn = connect(":memory:")
    return SqliteDiscoveredSlugsStore(conn), SqliteDiscoveredBoardsStore(conn), conn


def _cfg(**kw):
    base = dict(max_validations_per_run=600, quarantine_after_failures=5,
                manual_companies=[], revalidate_after_days=7,
                no_match_revalidate_after_days=90, yc_oss_enabled=False,
                revalidate_reserve=0)
    base.update(kw)
    return DiscoveryConfig(**base)


class _NoYc:
    async def fetch(self, client):
        return []


def _make_stale(conn, table, key_col, key):
    conn.execute(
        f"UPDATE {table} SET data = json_set(data, '$.last_swept_at', '2000-01-01T00:00:00+00:00',"
        f" '$.last_validated_at', '2000-01-01T00:00:00+00:00') WHERE {key_col} = ?", (key,))


# ---------- store: explicit origin on upsert_ok ----------

def test_slug_upsert_ok_preserves_origin_by_default_and_reclaims_on_request():
    slugs, _, _ = _stores()
    slugs.seed_ok("greenhouse:stripe", company_name="Stripe", origin=ORIGIN_US)
    slugs.upsert_ok("greenhouse:stripe", last_posting_count=3)
    assert slugs.get("greenhouse:stripe").origin == ORIGIN_US
    slugs.upsert_ok("greenhouse:stripe", last_posting_count=3, origin=None)
    assert slugs.get("greenhouse:stripe").origin is None
    slugs.upsert_ok("greenhouse:stripe", origin="hiringcafe")
    assert slugs.get("greenhouse:stripe").origin == "hiringcafe"


def test_board_upsert_ok_preserves_origin_by_default_and_reclaims_on_request():
    _, boards, _ = _stores()
    boards.seed_ok("acme.com", name="Acme", family="workday", identity=WD,
                   connector_name="workday:acme:Ext", company="Acme", origin=ORIGIN_US)
    boards.upsert_ok("acme.com", name="Acme", family="workday", identity=WD,
                     connector_name="workday:acme:Ext")
    assert boards.get("acme.com").origin == ORIGIN_US
    boards.upsert_ok("acme.com", name="Acme", family="workday", identity=WD,
                     connector_name="workday:acme:Ext", origin=None)
    assert boards.get("acme.com").origin is None


# ---------- discovery ----------

@pytest.mark.asyncio
async def test_pack_off_manual_chain_reclaims_hidden_starter_slug():
    slugs, boards, _ = _stores()
    slugs.seed_ok("greenhouse:stripe", company_name="Stripe", origin=ORIGIN_US)
    g_slugs, g_boards = gate_stores(OFF, slugs, boards)
    assert g_slugs.list_healthy() == []  # fixture sanity: hidden
    active = {(r.ats_family, r.slug) for r in g_slugs.list_healthy()}
    with patch("src.discovery._probe_all_ats", new=AsyncMock(return_value=("greenhouse", 9))):
        await run_discovery(client=None, yc_oss=_NoYc(), active_set=active, store=g_slugs,
                            cfg=_cfg(manual_companies=["stripe"]), boards=g_boards)
    assert [r.connector_name for r in g_slugs.list_healthy()] == ["greenhouse:stripe"]
    assert slugs.get("greenhouse:stripe").origin is None


@pytest.mark.asyncio
async def test_revalidation_of_starter_row_keeps_origin():
    slugs, boards, conn = _stores()
    slugs.seed_ok("greenhouse:stripe", company_name="Stripe", origin=ORIGIN_US)
    _make_stale(conn, "discovered_slugs", "connector_name", "greenhouse:stripe")
    g_slugs, g_boards = gate_stores(ON, slugs, boards)
    probe = AsyncMock(return_value=(True, 11))
    with patch("src.discovery._probe_one_ats", new=probe):
        await run_discovery(client=None, yc_oss=_NoYc(),
                            active_set={("greenhouse", "stripe")}, store=g_slugs,
                            cfg=_cfg(), boards=g_boards)
    assert probe.await_count == 1  # it was revalidated
    row = slugs.get("greenhouse:stripe")
    assert row.origin == ORIGIN_US and row.last_posting_count == 11


@pytest.mark.asyncio
async def test_slug_candidate_keeps_its_origin_even_when_rekeyed():
    slugs, boards, _ = _stores()
    slugs.upsert_candidate("greenhouse:newco", company_name="NewCo",
                           origin="hiringcafe", claimed_family="greenhouse")

    async def probe(*, client, ats_family, slug):
        return (True, 5) if ats_family == "lever" else (False, 0)

    with patch("src.discovery._probe_one_ats", side_effect=probe):
        await run_discovery(client=None, yc_oss=_NoYc(), active_set=set(),
                            store=slugs, cfg=_cfg())
    assert slugs.get("lever:newco").origin == "hiringcafe"


@pytest.mark.asyncio
async def test_candidate_chain_does_not_treat_hidden_starter_board_as_active():
    slugs, boards, _ = _stores()
    boards.seed_ok("acme.com", name="Acme", family="workday", identity=WD,
                   connector_name="workday:acme:Ext", company="Acme", origin=ORIGIN_US)
    g_slugs, g_boards = gate_stores(OFF, slugs, boards)
    fp = AsyncMock(return_value=FingerprintResult(
        name="Acme", domain="acme.com", status="matched", family="workday",
        identity=WD, posting_count=4))
    with patch("src.discovery._probe_all_ats", new=AsyncMock(return_value=None)), \
         patch("src.discovery.fingerprint_company", new=fp):
        _, outcome = await _run_candidate_chain(
            client=None, name="Acme", website="https://www.acme.com", alt_slug=None,
            store=g_slugs, boards=g_boards, active_set=set(), budget=100)
    assert outcome == "board_ok"
    assert [b.domain for b in g_boards.list_healthy()] == ["acme.com"]


@pytest.mark.asyncio
async def test_candidate_chain_still_skips_visible_starter_board():
    slugs, boards, _ = _stores()
    boards.seed_ok("acme.com", name="Acme", family="workday", identity=WD,
                   connector_name="workday:acme:Ext", company="Acme", origin=ORIGIN_US)
    g_slugs, g_boards = gate_stores(ON, slugs, boards)
    probe = AsyncMock()
    with patch("src.discovery._probe_all_ats", new=probe):
        _, outcome = await _run_candidate_chain(
            client=None, name="Acme", website="https://www.acme.com", alt_slug=None,
            store=g_slugs, boards=g_boards, active_set=set(), budget=100)
    assert outcome == "active"
    probe.assert_not_awaited()


@pytest.mark.asyncio
async def test_board_sweep_matching_starter_seed_domain_reclaims_it():
    slugs, boards, conn = _stores()
    boards.seed_ok("acme.com", name="Acme", family="workday", identity=WD,
                   connector_name="workday:acme:Ext", company="Acme", origin=ORIGIN_US)
    _make_stale(conn, "discovered_boards", "domain", "acme.com")

    async def fake_fp(client, seed):
        return FingerprintResult(name="Acme", domain="acme.com", status="matched",
                                 family="workday", identity=WD, posting_count=4)

    with patch("src.discovery.fingerprint_company", new=fake_fp):
        await run_board_discovery(client=None, seeds=[Seed(name="Acme", domain="acme.com")],
                                  boards=boards, max_sweeps_per_run=10,
                                  revalidate_after_days=14, quarantine_threshold=5)
    assert boards.get("acme.com").origin is None
    _, g_boards = gate_stores(OFF, slugs, boards)
    assert [b.domain for b in g_boards.list_healthy()] == ["acme.com"]


@pytest.mark.asyncio
async def test_board_sweep_miss_on_starter_row_keeps_origin():
    slugs, boards, conn = _stores()
    boards.seed_ok("acme.com", name="Acme", family="workday", identity=WD,
                   connector_name="workday:acme:Ext", company="Acme", origin=ORIGIN_US)
    _make_stale(conn, "discovered_boards", "domain", "acme.com")

    async def fake_fp(client, seed):
        return FingerprintResult(name="Acme", domain="acme.com", status="not_found")

    with patch("src.discovery.fingerprint_company", new=fake_fp):
        await run_board_discovery(client=None, seeds=[Seed(name="Acme", domain="acme.com")],
                                  boards=boards, max_sweeps_per_run=10,
                                  revalidate_after_days=14, quarantine_threshold=5)
    row = boards.get("acme.com")
    assert row.origin == ORIGIN_US and row.status == "ok"


@pytest.mark.asyncio
async def test_vc_capture_stages_company_whose_only_row_is_a_hidden_starter_row():
    from src.discovery import run_vc_discovery
    from src.state_sqlite import SqliteSourceStateStore
    from src.vc_portfolio import PortfolioCompany
    slugs, boards, conn = _stores()
    slugs.seed_ok("greenhouse:stripe", company_name="Stripe", origin=ORIGIN_US)
    g_slugs, g_boards = gate_stores(OFF, slugs, boards)
    with patch("src.vc_portfolio.a16z_portfolio",
               new=AsyncMock(return_value=[PortfolioCompany(name="Stripe", slug_candidates=[],
                                                            domain=None)])):
        await run_vc_discovery(client=None, firms=["a16z"], discovered=g_slugs, boards=g_boards,
                               source_state=SqliteSourceStateStore(conn), active_set=set(),
                               refresh_days=7, capture_cap=500, no_match_fresh_days=90)
    assert slugs.get("candidate:stripe") is not None
