import pytest

import src.starter_pack as sp
from src.starter_pack import StarterPack, PackSlug
from src.stores import build_stores
from tests.settings_helpers import seed_settings

PACK = StarterPack("t", (PackSlug("greenhouse", "stripe", "Stripe", "us", 5),), ())


@pytest.fixture
def pack(monkeypatch):
    monkeypatch.setattr(sp, "default_pack", lambda: PACK)
    monkeypatch.setattr("src.handler.default_pack", lambda: PACK)


def _capture(monkeypatch):
    seen = {}

    def fake_build(cfg, tier, *, discovered=None, boards=None, suppressed=frozenset(), sightings=None):
        seen["names"] = [r.connector_name for r in discovered.list_healthy()]
        seen["boards"] = boards
        return []

    monkeypatch.setattr("src.handler.build_connectors", fake_build)
    return seen


@pytest.mark.asyncio
async def test_ats_cycle_seeds_and_polls_pack_when_on(monkeypatch, pack):
    from src.handler import _run
    seed_settings({"discovery": {"starter_pack": True}})
    seen = _capture(monkeypatch)
    await _run(tier="ats")
    assert seen["names"] == ["greenhouse:stripe"]
    assert build_stores().discovered.get("greenhouse:stripe").origin == "starter"


@pytest.mark.asyncio
async def test_ats_cycle_neither_seeds_nor_polls_when_off(monkeypatch, pack):
    from src.handler import _run
    seed_settings({"discovery": {"starter_pack": False}})
    stores = build_stores()
    stores.discovered.seed_ok("greenhouse:stripe", company_name="Stripe", origin="starter")
    seen = _capture(monkeypatch)
    await _run(tier="ats")
    assert seen["names"] == []


@pytest.mark.asyncio
async def test_configured_and_starter_board_polled_once(monkeypatch, pack):
    """Real build_connectors: config wins the dedup, so one connector."""
    from src.handler import _run
    seed_settings({"discovery": {"starter_pack": True}, "sources": {"greenhouse": ["stripe"]}})
    names = []

    async def fake_run_once(**kw):
        names.extend(c.name for c in kw["connectors"])
        raise SystemExit  # stop after capture — nothing past run_once matters here

    monkeypatch.setattr("src.handler.run_once", fake_run_once)
    with pytest.raises(SystemExit):
        await _run(tier="ats")
    assert names.count("greenhouse:stripe") == 1


@pytest.mark.asyncio
async def test_discovery_tier_gates_active_set_but_recovers_on_raw_stores(monkeypatch, pack):
    """Pack off: a hidden starter row is not "already polled", so discovery may
    find the same company (I1); recovery still sees every row."""
    from src.handler import _run
    seed_settings({"discovery": {"enabled": True, "starter_pack": False,
                                 "board_discovery_enabled": False}})
    stores = build_stores()
    stores.discovered.seed_ok("greenhouse:stripe", company_name="Stripe", origin="starter")
    stores.discovered.upsert_ok("lever:mine")
    got = {}

    async def fake_discovery(*, client, yc_oss, active_set, store, cfg, boards):
        got["active"] = set(active_set)
        got["reval"] = store.list_for_revalidation(stale_after_days=-1, limit=10)
        got["boards_gated"] = hasattr(boards, "polls")

    async def fake_recover(*, cfg, discovered, boards, health, client):
        got["recover_names"] = [r.connector_name for r in discovered.list_healthy()]

    monkeypatch.setattr("src.handler.run_discovery", fake_discovery)
    monkeypatch.setattr("src.handler.recover_suppressed", fake_recover)
    await _run(tier="discovery")
    assert ("greenhouse", "stripe") not in got["active"]
    assert ("lever", "mine") in got["active"]
    assert [r.connector_name for r in got["reval"]] == ["lever:mine"]
    assert got["boards_gated"]
    assert sorted(got["recover_names"]) == ["greenhouse:stripe", "lever:mine"]


@pytest.mark.asyncio
async def test_discovery_tier_active_set_includes_starter_rows_when_pack_on(monkeypatch, pack):
    from src.handler import _run
    seed_settings({"discovery": {"enabled": True, "starter_pack": True,
                                 "board_discovery_enabled": False}})
    build_stores().discovered.seed_ok("greenhouse:stripe", company_name="Stripe", origin="starter")
    got = {}

    async def fake_discovery(*, client, yc_oss, active_set, store, cfg, boards):
        got["active"] = set(active_set)

    async def fake_recover(**kw):
        pass

    monkeypatch.setattr("src.handler.run_discovery", fake_discovery)
    monkeypatch.setattr("src.handler.recover_suppressed", fake_recover)
    await _run(tier="discovery")
    assert ("greenhouse", "stripe") in got["active"]
