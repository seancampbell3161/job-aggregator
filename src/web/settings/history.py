"""The History page: every saved settings version, what restoring one would
change, and a way back to it.

Registered separately from the generic /settings/{slug} catch-all, and
early — right alongside register_row_routes and register_companies_routes at
the top of register_settings_routes (routes.py's own module docstring) —
because /settings/history is a single path segment and would otherwise be
swallowed by that catch-all.

The list page (GET /settings/history) never computes a diff: it renders
lazily, each row's "what changed" link a plain <a href> (so it still works
with no JS) doubled as an hx-get against the SAME route, with hx-select
pulling just the #history-diff fragment out of the full page that comes
back. That is what keeps fifty stored versions from meaning fifty
diff_settings() calls on every page load — only the one version a person
actually opens gets diffed."""
from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from src.settings.diff import diff_settings
from src.settings.store import SettingsRow
from src.web.settings.sections import section_by_slug
from src.web.settings.shell import render_section

# Versions shown on /settings/history before the "show all" link appears.
# Matches ConfigService.versions()'s own default page size (not the `history`
# CLI command's --limit default of 20, which is a different, smaller number)
# so the web page and the service layer agree on what "a page" means.
_PAGE = 50


def _find(service, version_id: int) -> SettingsRow | None:
    """The stored row for this version id, or None — service.versions() has
    no by-id lookup of its own, and ConfigService.version_config() collapses
    "does not exist" and "exists but no longer validates" into the same
    None, which a 404 and a 409 need to tell apart."""
    return next((row for row in service.versions(limit=None) if row.id == version_id), None)


def register_history_routes(app: FastAPI) -> None:
    @app.get("/settings/history", response_class=HTMLResponse)
    def history_page(request: Request, all: bool = False):
        service = request.app.state.service
        rows = service.versions(limit=None if all else _PAGE)
        return render_section(
            request, section_by_slug("history"),
            rows=rows, row=None, in_effect=request.state.snapshot.version_id,
            showing_all=all or len(rows) < _PAGE,
        )

    @app.get("/settings/history/{version_id}", response_class=HTMLResponse)
    def history_version(request: Request, version_id: int):
        service = request.app.state.service
        row = _find(service, version_id)
        if row is None:
            raise HTTPException(status_code=404)
        cfg = service.version_config(version_id)
        # The page answers "what would change if I restored this" — the
        # version in effect is what is running NOW, so it is `old`; getting
        # this backwards renders a plausible but exactly inverted diff.
        lines = [] if cfg is None else diff_settings(request.state.snapshot.cfg, cfg)
        return render_section(
            request, section_by_slug("history"),
            rows=None, row=row, lines=lines, restorable=cfg is not None,
            in_effect=request.state.snapshot.version_id,
        )

    @app.post("/settings/history/{version_id}/restore")
    def history_restore(request: Request, version_id: int):
        """Gated the same way server-side as the button is hidden in the
        template: a version that no longer migrates/validates
        (version_config is None) is not restorable just because nobody
        clicked a button for it — a hand-made POST must 409, not silently
        restore whatever the stale row happens to contain."""
        service = request.app.state.service
        if _find(service, version_id) is None:
            raise HTTPException(status_code=404)
        if service.version_config(version_id) is None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"settings version {version_id} no longer validates "
                    "against the current settings schema and cannot be restored"
                ),
            )
        service.restore(version_id)
        return RedirectResponse("/settings/history?restored=1", status_code=303)
