"""Handler wiring for the polling scale-up (evaluation memory, cadence)."""
import pytest

from src.stores import build_stores
from tests.settings_helpers import seed_settings


async def _captured_kwargs(monkeypatch, tier):
    from src.handler import _run
    seed_settings({})
    got = {}

    async def fake_run_once(**kw):
        got.update(kw)
        raise SystemExit

    monkeypatch.setattr("src.handler.run_once", fake_run_once)
    monkeypatch.setattr("src.handler.build_connectors", lambda *a, **k: [])
    with pytest.raises(SystemExit):
        await _run(tier=tier)
    return got


@pytest.mark.asyncio
@pytest.mark.parametrize("tier", ["ats", "slow"])
async def test_ats_and_slow_get_evaluation_memory(monkeypatch, tier):
    got = await _captured_kwargs(monkeypatch, tier)
    stores = build_stores()
    assert isinstance(got["evaluated_store"], type(stores.evaluated))
    assert isinstance(got["generation"], int)


@pytest.mark.asyncio
async def test_headless_tier_is_exempt_from_evaluation_memory(monkeypatch):
    got = await _captured_kwargs(monkeypatch, "headless")
    assert got["evaluated_store"] is None and got["generation"] is None


@pytest.mark.asyncio
async def test_handler_reuses_one_pacer_per_tier(monkeypatch):
    from src import handler
    monkeypatch.setattr(handler, "_PACERS", {})
    a1 = await _captured_kwargs(monkeypatch, "ats")
    a2 = await _captured_kwargs(monkeypatch, "ats")
    s1 = await _captured_kwargs(monkeypatch, "slow")
    assert a1["pacer"] is a2["pacer"] and a1["pacer"] is not None
    assert s1["pacer"] is not a1["pacer"]
    assert a1["fetch_deadline_s"] > 0 and s1["fetch_deadline_s"] > 0


@pytest.mark.asyncio
async def test_headless_tier_is_not_paced(monkeypatch):
    got = await _captured_kwargs(monkeypatch, "headless")
    assert got["pacer"] is None and got["fetch_deadline_s"] is None
