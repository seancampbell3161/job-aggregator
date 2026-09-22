"""The Companies page: every configured board, grouped by family, with its
live status joined in from the runtime stores.

Registered separately from the generic /settings/{slug} catch-all (Ruling 1
on this branch) — /settings/companies is a single path segment, so the
catch-all would genuinely swallow it.

This module renders through shell.render_section, same as every other leaf
settings route module (Ruling R10) — not through routes.py, which used to be
where render_section (as `_render`) and page_ctx lived. That's what let
routes.py import this module at its own top level instead of lazily: this
module no longer imports anything from routes.py, so there is no cycle to
dodge."""
from __future__ import annotations

import json
import logging
from urllib.parse import quote

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

from src.fingerprint import (
    FingerprintResult, connector_name, gather_already_polled, normalize_target, probe_target,
)
from src.headless import headless_available
from src.settings.boards import BOARD_FAMILIES, BoardEntry, board_entries, board_key
from src.settings.errors import NotConfigured, SettingsInvalid, StaleWrite
from src.settings.fields import item_model
from src.settings.patch import apply_patch
from src.settings.rows import add_row_patch
from src.state import DiscoveredSlug
from src.web.auth import safe_next
from src.web.settings.health import board_status
from src.web.settings.sections import SECTIONS, section_by_slug
from src.web.settings.shell import render_section

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
    already: bool = False, error: str | None = None, next_path: str | None = None,
) -> HTMLResponse:
    # next_path is only ever a caller-supplied path already validated by
    # safe_next() in companies_probe below — this function trusts it as
    # given rather than re-validating, so it stays a plain pass-through for
    # whatever the "Add this board" form should return to once confirmed.
    # Named `next` in the template context (matching the form field name the
    # wizard's Check form submits), not `next_path`, so avoid the builtin
    # shadow here and rename only at the boundary.
    ctx: dict = {"result": result, "already": already, "error": error, "next": next_path}
    if result is not None:
        # connector_name() needs BOTH family and identity — an unsupported
        # result carries only a family, so this guard (not a template-side
        # one) is the single place that decides whether it is safe to call.
        has_identity = bool(result.family and result.identity)
        board_family = _board_family(result)
        ctx["board_family"] = board_family
        ctx["board_key_display"] = connector_name(result) if has_identity else None
        ctx["identity_json"] = json.dumps(result.identity) if has_identity else "{}"
        # Through _board_family, NOT result.family raw: result.family can be
        # the literal string "jsonld" (fingerprint_company's ambiguous
        # branch can set fam = supported[0][0], and a jsonld match's family
        # IS "jsonld") — _manual_row_path("jsonld") would build
        # "sources.jsonld", which is not in editable_row_paths() (the
        # config key is sources.jsonld_boards), 404ing the manual link.
        # _board_family already does exactly this "jsonld" ->
        # "jsonld_boards" translation; icims/successfactors/talentbrew
        # (never "jsonld" themselves) pass through it unchanged, so
        # _manual_row_path's own override for those three still applies.
        ctx["manual_path"] = _manual_row_path(board_family) if board_family else None
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


def _entry_by_digest(cfg, digest: str) -> BoardEntry:
    entry = next((e for e in board_entries(cfg) if e.digest == digest), None)
    if entry is None:
        raise HTTPException(status_code=404, detail="no such board")
    return entry


def _discovered_ok_row(stores, key: str) -> DiscoveredSlug | None:
    """The discovery row for this board, if discovery has independently
    validated it — fail-soft like board_status (src/web/settings/health.py):
    a locked telemetry table must not break the confirm page, only its
    warning. A "candidate"/"failed"/"no_match" row means discovery has not
    actually re-found this board yet, so it earns no warning either."""
    try:
        row = stores.discovered.get(key)
    except Exception as exc:  # noqa: BLE001 — telemetry never breaks a settings page
        log.warning("discovery_lookup_unavailable", extra={"error": str(exc)})
        return None
    return row if row is not None and row.validation_status == "ok" else None


def _block_name(entry: BoardEntry, row: DiscoveredSlug) -> str:
    """The name to prefill into the block-this-company field: the entry's own
    company (only the families whose element model has one — Workday,
    Avature, ...), else what discovery observed on the posting, else the
    board's on-screen label. A slug family (Greenhouse, Lever, ...) has no
    "company" field at all — its entry.values is just {"value": "<slug>"} —
    so it falls through to the discovered row's company_name when discovery
    has seen one, and otherwise to entry.label, which for a slug family IS
    the slug itself (board_label's bare-string branch)."""
    return entry.values.get("company") or row.company_name or entry.label


def register_companies_routes(app: FastAPI) -> None:
    @app.get("/settings/companies", response_class=HTMLResponse)
    def companies_page(request: Request):
        stores = request.app.state.stores
        cfg = request.state.snapshot.cfg
        entries = board_entries(cfg)
        configured = {entry.key for entry in entries}
        # Every write on this page lands back here via a redirect carrying a
        # flag (?added=1 from companies_add, ?changed=1/?removed=1 from the
        # generic row routes editing/removing a board, ?blocked=1 from
        # companies_block below) -- none of which anything used to read, so
        # settings_base.html's "Saved" banner never fired for any of them.
        # blocked gets its OWN message (below, in the template) instead of
        # the generic banner: blocking a company has no visible effect on
        # this page (filters.blocked_companies isn't rendered here), so the
        # generic "Saved — running live" text alone would leave the operator
        # with no evidence anything happened.
        blocked = request.query_params.get("blocked") or None
        saved = blocked is None and any(
            request.query_params.get(flag) == "1" for flag in ("added", "changed", "removed")
        )
        return render_section(
            request, section_by_slug("companies"),
            groups=_grouped(entries),
            status=board_status(stores),
            discovery_only=_discovery_only(stores, configured),
            board_families=BOARD_FAMILIES,
            headless_ok=headless_available(),
            saved=saved,
            blocked=blocked,
        )

    @app.post("/settings/companies/probe", response_class=HTMLResponse)
    async def companies_probe(request: Request):
        """Fingerprint the pasted value. Writes nothing, ever."""
        form = await request.form()
        target = str(form.get("target", ""))
        # `next` rides along from the Check form (a hidden field, set only by
        # callers that want confirming a board to land somewhere other than
        # this page — the wizard's companies step points it at /wizard) and
        # is threaded through to the "Add this board" form below, so
        # companies_add knows where to send the browser once it writes.
        # safe_next() rejects anything that isn't a same-site path, so a
        # tampered hidden field can't turn this into an open redirect.
        next_raw = form.get("next")
        next_path = safe_next(str(next_raw)) if next_raw else None
        try:
            normalize_target(target)
        except ValueError as exc:
            return _probe_partial(request, error=str(exc), next_path=next_path)
        async with _probe_client() as client:
            result = await probe_target(client, target)
        configured = gather_already_polled(request.state.snapshot.cfg)
        already = (result.status in ("matched", "not_found") and result.family
                   and result.identity and connector_name(result) in configured)
        return _probe_partial(request, result=result, already=bool(already), next_path=next_path)

    @app.post("/settings/companies/add", response_class=HTMLResponse)
    async def companies_add(request: Request):
        """Confirm one probed board. `family`, `name` and `identity` are form
        fields the browser sent back — no more trustworthy than hand-typed
        input, so every one of them is validated here or by add_row_patch."""
        form = await request.form()
        family = str(form.get("family", ""))
        name = str(form.get("name", ""))
        # Where to land after a successful add. Defaults to this same page
        # (?added=1, read by companies_page's "Saved" banner) unless a
        # caller asked for somewhere else — see companies_probe above for
        # where `next` comes from. Re-checked with safe_next() here too: the
        # hidden field survives in the browser's DOM, so a POST straight to
        # this route (bypassing the probe step) is a tampering vector this
        # route must not trust blindly even though companies_probe already
        # validated the value it originally handed back.
        next_raw = form.get("next")
        next_path = safe_next(str(next_raw)) if next_raw else None
        added_url = next_path or "/settings/companies?added=1"
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
            return RedirectResponse(added_url, status_code=303)

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
        return RedirectResponse(added_url, status_code=303)

    @app.get("/settings/companies/remove/{digest}", response_class=HTMLResponse)
    def companies_remove_confirm(request: Request, digest: str):
        """A companies-specific confirm page: same removal as the generic
        /settings/rows/{path}/{digest}/remove (Ruling R8 — this page's own
        form posts THERE, it does not add a second way to remove a board),
        but with the one thing that route has no business knowing: whether
        discovery independently polls this same board and would happily
        rediscover it the moment it is gone. Writes nothing — even a re-run
        of the discovery lookup on every render is read-only."""
        entry = _entry_by_digest(request.state.snapshot.cfg, digest)
        row = _discovered_ok_row(request.app.state.stores, entry.key)
        return request.app.state.templates.TemplateResponse(
            request, "_company_remove.html",
            {
                "sections": SECTIONS,
                "section": section_by_slug("companies"),
                "saved": False,
                "form_errors": [],
                "entry": entry,
                "discovered": row,
                "block_name": _block_name(entry, row) if row is not None else None,
            },
        )

    @app.post("/settings/companies/block")
    async def companies_block(request: Request):
        """Append to filters.blocked_companies — token-matched against a
        posting's company name at score time (src/filters.py::filter_company),
        NOT against a board's slug or key. That mismatch is exactly why the
        confirm page prefills this rather than deriving and submitting it
        invisibly: the name that actually stops future postings can differ
        from the board's address, so the human blocking it gets a chance to
        fix it first."""
        form = await request.form()
        company = str(form.get("company", "")).strip()
        if not company:
            return PlainTextResponse("a company name is required", status_code=400)

        def mutate(doc: dict) -> str | None:
            blocked = doc.setdefault("filters", {}).setdefault("blocked_companies", [])
            if company in blocked:
                return None
            blocked.append(company)
            return f"ui: blocked {company}"

        try:
            request.app.state.service.update_settings(mutate, source="ui")
        except StaleWrite:
            # Every other write path in this module (companies_add) and in
            # rows.py's _apply reports this instead of letting it 500 — a
            # concurrent settings save is routine, not exceptional.
            raise HTTPException(
                status_code=409,
                detail="Someone else saved while this was open. Reload and try again.",
            )
        request.state.snapshot = request.app.state.service.snapshot()
        # The company name rides along in the query string (not just a bare
        # ?blocked=1) so companies_page can say WHICH company was blocked --
        # the page has no other way to show it, since it doesn't render
        # filters.blocked_companies at all.
        return RedirectResponse(f"/settings/companies?blocked={quote(company)}", status_code=303)
