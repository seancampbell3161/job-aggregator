"""Template values every page reads from the request's settings snapshot."""
from __future__ import annotations

from fastapi import Request


def config_ctx(request: Request) -> dict:
    """Score thresholds and board staleness from request.state.snapshot (set by
    the setup gate; always present on routes outside the exempt prefixes)."""
    cfg = request.state.snapshot.cfg
    return {
        "score_high": cfg.relevance.score_high,
        "score_low": cfg.relevance.score_low,
        "stale_after_days": cfg.board.stale_after_days,
    }
