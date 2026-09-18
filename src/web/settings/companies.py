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

import json
import logging

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from src.fingerprint import (
    FingerprintResult, connector_name, gather_already_polled, normalize_target, probe_target,
)
from src.settings.boards import BOARD_FAMILIES, BoardEntry, board_entries, board_key
from src.settings.errors import NotConfigured, SettingsInvalid, StaleWrite
from src.settings.fields import item_model
from src.settings.rows import add_row_patch
from src.web.settings.forms import apply_patch
from src.web.settings.health import board_status
from src.web.settings.routes import _render
from src.web.settings.sections import section_by_slug

log = logging.getLogger(__name__)

PROBE_TIMEOUT = 20.0

# icims/successfactors/talentbrew are only ever addable hand-curated, into
# sources.jsonld_boards (fingerprint.py's own docstring) — none of the three
# is itself a BOARD_FAMILIES member, so an unsupported card naming one of
# them must not point its "add manually" link at a sources.<family> path
# that does not exist.
_JSONLD_UNSUPPORTED_FAMILIES = frozenset({"icims", "successfactors", "talentbrew"})


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


def _probe_client() -> httpx.AsyncClient:
    """A seam for tests; production uses httpx's default transport."""
    return httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(PROBE_TIMEOUT))


def _board_family(result: FingerprintResult) -> str | None:
    """The sources-config key for a probed family — identical to
    result.family except "jsonld", whose matches are stored under
    sources.jsonld_boards (config_entry's own naming; see fingerprint.py).
    None when the result carries no family at all (e.g. a plain not_found)."""
    if not result.family:
        return None
    return "jsonld_boards" if result.family == "jsonld" else result.family


def _manual_row_path(family: str) -> str:
    """Where an unsupported family's "add it manually" link points."""
    if family in _JSONLD_UNSUPPORTED_FAMILIES:
        return "sources.jsonld_boards"
    return f"sources.{family}"


def _probe_partial(
    request: Request, *, result: FingerprintResult | None = None,
    already: bool = False, error: str | None = None,
) -> HTMLResponse:
    ctx: dict = {"result": result, "already": already, "error": error}
    if result is not None:
        # connector_name() needs BOTH family and identity — an unsupported
        # result carries only a family, so this guard (not a template-side
        # one) is the single place that decides whether it is safe to call.
        has_identity = bool(result.family and result.identity)
        ctx["board_family"] = _board_family(result)
        ctx["board_key_display"] = connector_name(result) if has_identity else None
        ctx["identity_json"] = json.dumps(result.identity) if has_identity else "{}"
        ctx["manual_path"] = _manual_row_path(result.family) if result.family else None
    return request.app.state.templates.TemplateResponse(
        request, "_company_probe.html", ctx,
    )


def _row_values(family: str, identity: dict, name: str) -> dict:
    """The item-relative values add_row_patch expects for one family.

    A slug family's element has no model (rows.py's chips branch) — its one
    synthetic field is "value". Every other family's identity dict already
    matches its element model's fields one-for-one (it came from the same
    fingerprint.py identity that config_entry() builds settings entries
    from); the only thing this route adds on top is the company label, and
    only for the models that actually have one — Task 3's board_label and
    config_entry() agree on exactly which families that is."""
    path = f"sources.{family}"
    model = item_model(path)
    if model is None:
        return {"value": identity.get("slug", "")}
    values = dict(identity)
    if "company" in model.model_fields:
        values["company"] = name
    return values


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

    @app.post("/settings/companies/probe", response_class=HTMLResponse)
    async def companies_probe(request: Request):
        """Fingerprint the pasted value. Writes nothing, ever."""
        form = await request.form()
        target = str(form.get("target", ""))
        try:
            normalize_target(target)
        except ValueError as exc:
            return _probe_partial(request, error=str(exc))
        async with _probe_client() as client:
            result = await probe_target(client, target)
        configured = gather_already_polled(request.state.snapshot.cfg)
        already = (result.status in ("matched", "not_found") and result.family
                   and result.identity and connector_name(result) in configured)
        return _probe_partial(request, result=result, already=bool(already))

    @app.post("/settings/companies/add", response_class=HTMLResponse)
    async def companies_add(request: Request):
        """Confirm one probed board. `family`, `name` and `identity` are form
        fields the browser sent back — no more trustworthy than hand-typed
        input, so every one of them is validated here or by add_row_patch."""
        form = await request.form()
        family = str(form.get("family", ""))
        name = str(form.get("name", ""))
        if family not in BOARD_FAMILIES:
            raise HTTPException(status_code=400, detail="unknown source family")
        try:
            identity = json.loads(str(form.get("identity", "")))
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="malformed identity")
        if not isinstance(identity, dict):
            raise HTTPException(status_code=400, detail="malformed identity")

        cfg = request.state.snapshot.cfg
        path = f"sources.{family}"
        values = _row_values(family, identity, name)
        try:
            patch = add_row_patch(cfg, path, values)
        except SettingsInvalid as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        # Dedupe runs AFTER add_row_patch, against the canonical entry it just
        # built (the last item of the new list) — never before it. board_key()
        # indexes required keys directly (entry['tenant'], entry['site'], ...)
        # with no defensive .get(), which is fine once add_row_patch's own
        # pydantic validation has guaranteed they're all present; running this
        # check first, against the raw submitted (possibly incomplete)
        # `values`, would let a tampered identity missing a required key raise
        # an uncaught KeyError here instead of the clean 400 above.
        new_entry = patch[path][-1]
        if board_key(family, new_entry) in gather_already_polled(cfg):
            return RedirectResponse("/settings/companies?added=1", status_code=303)

        def mutate(doc: dict) -> str | None:
            return f"ui: added a {family} board" if apply_patch(doc, patch) else None

        try:
            request.app.state.service.update_settings(mutate, source="ui")
        except SettingsInvalid as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except StaleWrite:
            raise HTTPException(
                status_code=409,
                detail="Someone else saved while this was probing. Reload and try again.",
            )
        except NotConfigured:
            return RedirectResponse("/setup", status_code=303)

        request.state.snapshot = request.app.state.service.snapshot()
        return RedirectResponse("/settings/companies?added=1", status_code=303)
