"""Fetch-step tests for run_vc_discovery (spec part 3: VC driver automation).
Fixture-based — drivers are patched at src.vc_portfolio (the in-function
import in run_vc_discovery resolves the patched attribute at call time)."""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from src.discovery import run_vc_discovery
from src.models import ConnectorState
from src.sqlite_db import connect
from src.state_sqlite import (
    SqliteDiscoveredBoardsStore,
    SqliteDiscoveredSlugsStore,
    SqliteSourceStateStore,
)
from src.vc_portfolio import PortfolioCompany


def _stores():
    return (SqliteDiscoveredSlugsStore(connect(":memory:")),
            SqliteSourceStateStore(connect(":memory:")))


def _co(name, domain=None):
    return PortfolioCompany(name=name, slug_candidates=[], domain=domain)


async def _run(firms, *, discovered, source_state, boards=None, active_set=None,
               refresh_days=7, capture_cap=500, no_match_fresh_days=90):
    await run_vc_discovery(
        client=None, firms=firms, discovered=discovered, boards=boards,
        source_state=source_state, active_set=active_set or set(),
        refresh_days=refresh_days, capture_cap=capture_cap,
        no_match_fresh_days=no_match_fresh_days,
    )


def _backdate_nomatch(store, slug, iso):
    key = f"nomatch:{slug}"
    row = store._conn.execute(
        "SELECT data FROM discovered_slugs WHERE connector_name = ?", (key,)
    ).fetchone()
    d = json.loads(row["data"])
    d["last_validated_at"] = iso
    store._conn.execute(
        "UPDATE discovered_slugs SET data = ? WHERE connector_name = ?",
        (json.dumps(d), key),
    )


# ---------- staging ----------

@pytest.mark.asyncio
async def test_fetch_stages_candidate_rows_with_origin_and_website():
    discovered, source_state = _stores()
    companies = [_co("Acme Inc.", "acme.com"), _co("Widget Labs")]
    with patch("src.vc_portfolio.a16z_portfolio", new=AsyncMock(return_value=companies)):
        await _run(["a16z"], discovered=discovered, source_state=source_state)
    row = discovered.get("candidate:acme")   # "Acme Inc." → punct strip + -inc strip
    assert row.validation_status == "candidate"
    assert row.company_name == "Acme Inc." and row.website == "acme.com"
    assert row.origin == "vc:a16z" and row.sighted_at is not None
    # sequoia-style no-domain entry stages under the name slug alone
    no_domain = discovered.get("candidate:widget-labs")
    assert no_domain is not None and no_domain.website is None


@pytest.mark.asyncio
async def test_capture_cap_per_firm_and_cap_is_not_failure():
    discovered, source_state = _stores()
    companies = [_co(f"Company {i}") for i in range(5)]
    with patch("src.vc_portfolio.a16z_portfolio", new=AsyncMock(return_value=companies)):
        await _run(["a16z"], discovered=discovered, source_state=source_state,
                   capture_cap=3)
    assert len(discovered.list_candidates()) == 3
    assert source_state.get("vc:a16z").last_modified is not None  # cap ≠ failure


@pytest.mark.asyncio
async def test_empty_firms_is_a_noop():
    discovered, source_state = _stores()
    await _run([], discovered=discovered, source_state=source_state)
    assert discovered.list_candidates() == []


# ---------- watermark ----------

@pytest.mark.asyncio
async def test_fresh_watermark_skips_fetch():
    discovered, source_state = _stores()
    source_state.put("vc:a16z", ConnectorState(
        last_modified=datetime.now(timezone.utc).isoformat()))
    fetch = AsyncMock(return_value=[_co("Acme", "acme.com")])
    with patch("src.vc_portfolio.a16z_portfolio", new=fetch):
        await _run(["a16z"], discovered=discovered, source_state=source_state)
    fetch.assert_not_awaited()
    assert discovered.list_candidates() == []


@pytest.mark.asyncio
async def test_stale_watermark_runs_and_advances_on_success():
    discovered, source_state = _stores()
    old = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    source_state.put("vc:a16z", ConnectorState(last_modified=old))
    with patch("src.vc_portfolio.a16z_portfolio",
               new=AsyncMock(return_value=[_co("Acme", "acme.com")])):
        await _run(["a16z"], discovered=discovered, source_state=source_state)
    assert len(discovered.list_candidates()) == 1
    assert source_state.get("vc:a16z").last_modified > old


@pytest.mark.asyncio
async def test_empty_portfolio_is_failure_watermark_not_advanced():
    """An empty portfolio page is a scrape break, not a signal."""
    discovered, source_state = _stores()
    with patch("src.vc_portfolio.a16z_portfolio", new=AsyncMock(return_value=[])):
        await _run(["a16z"], discovered=discovered, source_state=source_state)
    assert source_state.get("vc:a16z").last_modified is None
    assert discovered.list_candidates() == []


# ---------- fail-soft ----------

@pytest.mark.asyncio
async def test_driver_failure_isolated_per_firm():
    discovered, source_state = _stores()
    with patch("src.vc_portfolio.a16z_portfolio",
               new=AsyncMock(side_effect=RuntimeError("markup changed"))), \
         patch("src.vc_portfolio.sequoia_portfolio",
               new=AsyncMock(return_value=[_co("Seqco")])):
        await _run(["a16z", "sequoia"], discovered=discovered, source_state=source_state)
    assert source_state.get("vc:a16z").last_modified is None       # failed: retry next run
    assert source_state.get("vc:sequoia").last_modified is not None
    assert discovered.get("candidate:seqco").origin == "vc:sequoia"


@pytest.mark.asyncio
async def test_unknown_firm_logged_and_skipped():
    discovered, source_state = _stores()
    with patch("src.vc_portfolio.a16z_portfolio",
               new=AsyncMock(return_value=[_co("Acme", "acme.com")])):
        await _run(["lightspeed", "a16z"], discovered=discovered, source_state=source_state)
    assert discovered.get("candidate:acme") is not None            # a16z still ran
    assert source_state.get("vc:lightspeed").last_modified is None


@pytest.mark.asyncio
async def test_store_write_error_fail_soft_per_entry():
    discovered, source_state = _stores()
    real = discovered.upsert_candidate
    calls = {"n": 0}

    def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("disk full")
        return real(*a, **kw)

    discovered.upsert_candidate = flaky
    with patch("src.vc_portfolio.a16z_portfolio",
               new=AsyncMock(return_value=[_co("Alpha"), _co("Beta")])):
        await _run(["a16z"], discovered=discovered, source_state=source_state)
    assert discovered.get("candidate:beta") is not None
    assert source_state.get("vc:a16z").last_modified is not None   # run still "succeeded"


# ---------- dedup ----------

@pytest.mark.asyncio
async def test_dedup_active_set_family_rows_candidates_and_recent_nomatch():
    discovered, source_state = _stores()
    discovered.upsert_ok("lever:widget", company_name="Widget")    # family row exists
    discovered.upsert_candidate("candidate:gadget")                # already staged
    discovered.upsert_no_match("doohickey")                        # recent nomatch
    companies = [_co("Acme"), _co("Widget"), _co("Gadget"), _co("Doohickey")]
    with patch("src.vc_portfolio.a16z_portfolio", new=AsyncMock(return_value=companies)):
        await _run(["a16z"], discovered=discovered, source_state=source_state,
                   active_set={("greenhouse", "acme")})
    assert discovered.get("candidate:acme") is None                # active set
    assert discovered.get("candidate:widget") is None              # lever:widget exists
    assert discovered.get("candidate:gadget").origin is None       # pre-existing row untouched
    assert discovered.get("candidate:doohickey") is None           # recent nomatch


@pytest.mark.asyncio
async def test_expired_nomatch_restages():
    """A fresh portfolio sighting of a long-expired miss is a signal — re-stage."""
    discovered, source_state = _stores()
    discovered.upsert_no_match("acme")
    _backdate_nomatch(discovered, "acme", "2000-01-01T00:00:00+00:00")
    with patch("src.vc_portfolio.a16z_portfolio",
               new=AsyncMock(return_value=[_co("Acme")])):
        await _run(["a16z"], discovered=discovered, source_state=source_state)
    assert discovered.get("candidate:acme") is not None


@pytest.mark.asyncio
async def test_board_domain_dedups():
    discovered, source_state = _stores()
    boards = SqliteDiscoveredBoardsStore(connect(":memory:"))
    boards.upsert_ok("acme.com", name="Acme", family="workday",
                     identity={"tenant": "acme", "region": "wd5", "site": "Ext"},
                     connector_name="workday:acme:Ext", company="Acme")
    with patch("src.vc_portfolio.a16z_portfolio",
               new=AsyncMock(return_value=[_co("Acme Inc.", "acme.com")])):
        await _run(["a16z"], discovered=discovered, source_state=source_state,
                   boards=boards)
    assert discovered.list_candidates() == []
