"""A one-off, bounded, read-only poll: what WOULD arrive, right now.

Read-only three times over — dry_run=True, ignore_seen=True, and no sinks —
because the alternative is worse than showing nothing. If the preview marked
postings as seen, or advanced a connector's ETag, the user's first real cycle
would find nothing new and their first alert batch would vanish without a
trace. See Task 15 and the spec."""
from __future__ import annotations

import asyncio
import logging

import httpx

from src.connectors.base import build_connectors
from src.orchestrator import run_once
from src.starter_pack import default_pack, gate_stores, reconcile

log = logging.getLogger(__name__)

# Bounded so a first-time user is not left staring at a spinner while every
# configured board is polled. Partial results are shown either way.
MAX_BOARDS = 5
BUDGET_SECONDS = 60


def order_for_preview(connectors: list, ranks: dict[str, int]) -> list:
    """The user's own boards first (built order), then starter boards with the
    most postings — five slots should show what the pack is worth, not
    whichever tiny board sorts first."""
    mine = [c for c in connectors if c.name not in ranks]
    starter = sorted((c for c in connectors if c.name in ranks), key=lambda c: -ranks[c.name])
    return mine + starter


async def run_preview(app) -> dict:
    """Run the preview and store its record. Never raises: a preview failure
    is a message on a page, not a broken wizard."""
    store = app.state.stores.wizard
    store.put("preview", {"status": "running"})
    try:
        record = await asyncio.wait_for(_run(app), timeout=BUDGET_SECONDS)
    except asyncio.TimeoutError:
        record = {"status": "error", "error":
                  f"The preview ran longer than {BUDGET_SECONDS}s and was stopped."}
    except Exception as exc:  # noqa: BLE001 — a preview never breaks the wizard
        log.warning("wizard_preview_failed", extra={"error": str(exc)})
        record = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
    store.put("preview", record)
    return record


async def _run(app) -> dict:
    from src.handler import _build_relevance_scorer

    snap = app.state.service.snapshot()
    stores = app.state.stores
    cfg = snap.cfg

    pack = default_pack()
    if cfg.discovery.starter_pack:
        # The poller may not have run a cycle since the wizard turned the
        # pack on; seed now so the preview shows it.
        reconcile(pack, eu_enabled=cfg.discovery.eu_seeds_enabled,
                  slugs_store=stores.discovered, boards_store=stores.boards)
    discovered, boards = gate_stores(cfg, stores.discovered, stores.boards)
    connectors = order_for_preview(list(build_connectors(
        cfg, tier="ats", discovered=discovered, boards=boards, suppressed=frozenset(),
    )), pack.postings_by_connector())[:MAX_BOARDS]

    result = await run_once(
        cfg=cfg, tier="ats", store=stores.seen, source_state=stores.source_state,
        connectors=connectors,
        sinks=[],                    # nothing to deliver to, by construction
        client_factory=lambda: httpx.AsyncClient(),
        dry_run=True,                # no seen writes, no cursor writes
        ignore_seen=True,            # show matches even if the poller ran first
        relevance_scorer=_build_relevance_scorer(cfg, snap.documents.profile),
        gap_analyzer=None,           # not worth an LLM call per posting here
        health=None,
        rejected_store=None,
    )
    return {
        "status": "ok",
        "fetched": result.fetched_count,
        "matched": result.matched_count,
        "matches": [
            {
                "title": p.role, "company": p.company, "location": p.location,
                "apply_url": p.apply_url, "score": p.relevance_score,
            }
            for p in result.would_notify[:25]
        ],
    }
