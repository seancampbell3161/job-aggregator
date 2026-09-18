"""The Companies page: every configured board, grouped by family, with its
live status joined in from the runtime stores.

Registered separately from the generic /settings/{slug} catch-all (Ruling 1
on this branch) — /settings/companies is a single path segment, so the
catch-all would genuinely swallow it. This module is imported lazily from
register_settings_routes (not at src.web.settings.routes' own module top)
specifically to avoid that: this module imports `_render` back out of
routes.py, and importing companies.py at routes.py's top level would make
that a real circular import. `section_by_slug` itself comes straight from
sections.py (the layering this branch established — rows.py does the same,
per Ruling R9), so it is `_render` alone that forces the lazy import here.

Per Ruling R9, `_render` is NOT similarly hoisted in this fix round: it wraps
page_ctx, which depends on routes.py's own _SINK_PROBES registry, and
tests/web/settings/test_probes.py monkeypatches probe functions by that
module's path — moving the registry now risks quietly breaking those tests.
Task 11 is expected to move page_ctx/render_section into a shared shell.py so
no leaf settings module needs to import from routes.py at all."""
from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

from src.settings.boards import BOARD_FAMILIES, BoardEntry, board_entries
from src.web.settings.health import board_status
from src.web.settings.routes import _render
from src.web.settings.sections import section_by_slug

log = logging.getLogger(__name__)


def _discovery_only(stores, configured: set[str]) -> int:
    """How many slugs discovery has found and validated that are not already
    one of the boards this instance polls directly — a nudge toward
    /pipeline, not a listing (Task 7 owns configured boards only)."""
    try:
        return sum(
            1 for r in stores.discovered.list_healthy() if r.connector_name not in configured
        )
    except Exception as exc:  # noqa: BLE001 — telemetry never breaks a settings page
        log.warning("discovery_only_count_unavailable", extra={"error": str(exc)})
        return 0


def _grouped(entries: list[BoardEntry]) -> list[tuple[str, list[BoardEntry]]]:
    """Entries bucketed by family, in BOARD_FAMILIES display order. A family
    with nothing configured gets no group at all, so the empty-instance case
    is "no groups" rather than sixteen empty tables."""
    by_family: dict[str, list[BoardEntry]] = {}
    for entry in entries:
        by_family.setdefault(entry.family, []).append(entry)
    return [(family, by_family[family]) for family in BOARD_FAMILIES if family in by_family]


def register_companies_routes(app: FastAPI) -> None:
    @app.get("/settings/companies", response_class=HTMLResponse)
    def companies_page(request: Request):
        stores = request.app.state.stores
        cfg = request.state.snapshot.cfg
        entries = board_entries(cfg)
        configured = {entry.key for entry in entries}
        return _render(
            request, section_by_slug("companies"),
            groups=_grouped(entries),
            status=board_status(stores),
            discovery_only=_discovery_only(stores, configured),
        )
