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
    from src.version import APP_VERSION
    assert got["app_version"] == APP_VERSION


@pytest.mark.asyncio
async def test_headless_tier_is_exempt_from_evaluation_memory(monkeypatch):
    got = await _captured_kwargs(monkeypatch, "headless")
    assert got["evaluated_store"] is None and got["generation"] is None
    assert got["app_version"] is None


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


# --- adaptive cadence -------------------------------------------------------

T0 = 1_800_000_000.0
ATS_INTERVAL_S = 600  # default schedules.ats_minutes == 10


async def _cycle(monkeypatch, *, at_s, tier="ats", polled=None, deferred=(), paced_out=(),
                 connectors=(), dry_run=False, calibrate=False):
    """One handler cycle at wall time ``at_s``; returns the `suppressed` set that
    build_connectors received."""
    import time as _time
    from types import SimpleNamespace
    from src.handler import _run
    from src.orchestrator import RunResult

    seen = {}

    def fake_build(cfg, tier, *, discovered=None, boards=None, suppressed=frozenset(), sightings=None):
        seen["suppressed"] = set(suppressed)
        return [SimpleNamespace(name=n) for n in connectors]

    async def fake_run_once(**kw):
        return RunResult(polled=dict(polled or {}), deferred_sources=list(deferred),
                         paced_out=list(paced_out))

    monkeypatch.setattr(_time, "time", lambda: at_s)
    monkeypatch.setattr("src.handler.build_connectors", fake_build)
    monkeypatch.setattr("src.handler.run_once", fake_run_once)
    await _run(tier=tier, dry_run=dry_run, calibrate=calibrate)
    return seen["suppressed"]


@pytest.mark.asyncio
async def test_quiet_board_skipped_until_due(monkeypatch):
    seed_settings({})
    await _cycle(monkeypatch, at_s=T0, polled={"greenhouse:a": 0})
    # quiet board doubled to 1200 s: not due 600 s later, due after 1200 s
    assert "greenhouse:a" in await _cycle(monkeypatch, at_s=T0 + ATS_INTERVAL_S)
    assert "greenhouse:a" not in await _cycle(monkeypatch, at_s=T0 + 2 * ATS_INTERVAL_S)


@pytest.mark.asyncio
async def test_board_with_new_postings_due_next_cycle(monkeypatch):
    seed_settings({})
    await _cycle(monkeypatch, at_s=T0, polled={"greenhouse:a": 3, "greenhouse:b": 0})
    sup = await _cycle(monkeypatch, at_s=T0 + ATS_INTERVAL_S)
    assert "greenhouse:a" not in sup
    assert "greenhouse:b" in sup


@pytest.mark.asyncio
async def test_generation_change_makes_every_board_due(monkeypatch):
    seed_settings({})
    await _cycle(monkeypatch, at_s=T0, polled={"greenhouse:a": 0})
    assert "greenhouse:a" in await _cycle(monkeypatch, at_s=T0 + 60)
    seed_settings({"relevance": {"score_low": 5}})  # settings edit -> new generation
    sup = await _cycle(monkeypatch, at_s=T0 + 120)
    assert "greenhouse:a" not in sup


@pytest.mark.asyncio
async def test_cap_deferred_board_is_due_next_cycle(monkeypatch):
    seed_settings({})
    await _cycle(monkeypatch, at_s=T0, polled={"x": 0, "y": 0}, deferred=["x"])
    sup = await _cycle(monkeypatch, at_s=T0 + ATS_INTERVAL_S)
    assert "x" not in sup
    assert "y" in sup


@pytest.mark.asyncio
async def test_failed_fetch_leaves_schedule_alone(monkeypatch):
    seed_settings({})
    await _cycle(monkeypatch, at_s=T0, polled={"greenhouse:a": 0})
    before = build_stores().schedule.get("greenhouse:a")
    assert before is not None
    # cycle 2: the board is absent from polled (failed fetch) -> row untouched
    await _cycle(monkeypatch, at_s=T0 + 2 * ATS_INTERVAL_S, polled={})
    assert build_stores().schedule.get("greenhouse:a") == before


@pytest.mark.asyncio
async def test_paced_out_board_stays_due(monkeypatch):
    seed_settings({})
    await _cycle(monkeypatch, at_s=T0, polled={"a": 0}, paced_out=["p"])
    sup = await _cycle(monkeypatch, at_s=T0 + ATS_INTERVAL_S)
    assert "p" not in sup
    assert build_stores().schedule.get("p") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", ["dry_run", "calibrate"])
async def test_dry_run_ignores_and_never_writes_schedule(monkeypatch, flag):
    seed_settings({})
    await _cycle(monkeypatch, at_s=T0, polled={"greenhouse:a": 0})
    before = build_stores().schedule.get("greenhouse:a")
    sup = await _cycle(monkeypatch, at_s=T0 + 60, polled={"greenhouse:a": 5, "new": 1},
                       **{flag: True})
    assert "greenhouse:a" not in sup  # not-due set not applied
    assert build_stores().schedule.get("greenhouse:a") == before
    assert build_stores().schedule.get("new") is None


@pytest.mark.asyncio
async def test_headless_tier_has_no_cadence(monkeypatch):
    seed_settings({})
    await _cycle(monkeypatch, at_s=T0, polled={"greenhouse:a": 0})
    sup = await _cycle(monkeypatch, at_s=T0 + 60, tier="headless", polled={"hl": 0})
    assert "greenhouse:a" not in sup
    assert build_stores().schedule.get("hl") is None


@pytest.mark.asyncio
async def test_schedule_failure_never_fails_the_cycle(monkeypatch):
    seed_settings({})
    from src.state_sqlite import SqliteConnectorScheduleStore

    def boom(*a, **k):
        raise RuntimeError("db locked")

    monkeypatch.setattr(SqliteConnectorScheduleStore, "not_due", boom)
    monkeypatch.setattr(SqliteConnectorScheduleStore, "record_polls", boom)
    await _cycle(monkeypatch, at_s=T0, polled={"a": 0})  # must not raise


@pytest.mark.asyncio
async def test_generation_change_cycle_forces_unpolled_boards_due(monkeypatch):
    seed_settings({})
    # Build a long (quiet) schedule for the board under the current generation.
    for i in range(3):
        await _cycle(monkeypatch, at_s=T0 + i * 3600, polled={"greenhouse:a": 0},
                     connectors=["greenhouse:a"])
    assert "greenhouse:a" in await _cycle(monkeypatch, at_s=T0 + 2 * 3600 + 60)
    seed_settings({"relevance": {"score_low": 5}})  # new generation
    t = T0 + 2 * 3600 + 120
    # the board is attempted this cycle but not polled (failed / paced out)
    await _cycle(monkeypatch, at_s=t, polled={}, connectors=["greenhouse:a"])
    sup = await _cycle(monkeypatch, at_s=t + ATS_INTERVAL_S)
    assert "greenhouse:a" not in sup
