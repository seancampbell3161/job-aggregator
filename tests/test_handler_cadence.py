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
