"""Add, edit and remove one entry of a list-of-model settings field.

Generic over the dotted path, so the Companies page and an Advanced group use
the same routes. Writes go through update_settings as a patch whose value is
the WHOLE new list (src/settings/rows.py builds it), so an edit to one family
cannot revert an edit to another.

Per Ruling R7, the new/edit pages are STANDALONE PAGES (they extend
settings_base.html), not htmx partials: settings_advanced_group.html wraps
every field it renders in one outer <form>, so a row <form> swapped inside it
would be a nested form — browsers drop those, silently. On success the caller
is redirected (303) back to the page that owns the path; on a validation
failure the standalone page re-renders with the SUBMITTED values and an
inline error, so nothing typed is lost.

Per Ruling R8, Remove is the same story one level up: _rows.html cannot even
have an inline <form> for it (same nested-form hazard), so Remove is a plain
<a> to a GET confirm PAGE (row_remove_confirm below) whose own standalone
<form> POSTs to the mutating route. A formaction/formmethod button was
rejected because it is inert outside of *some* <form> — it would only have
worked by accident of currently always being included inside one, and Task
7's Companies page has no outer form at all."""
from __future__ import annotations

from typing import Any, Mapping

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from src.config import SLUG_SOURCE_FAMILIES
from src.settings.boards import BOARD_FAMILIES
from src.settings.errors import NotConfigured, SettingsInvalid, StaleWrite
from src.settings.fields import KIND_TEXT, item_fields, rows_paths
from src.settings.rows import (
    RowGone, add_row_patch, find_row, remove_row_patch, update_row_patch,
)
from src.web.settings.forms import apply_patch, decode, errors_by_path
from src.web.settings.sections import (
    SECTIONS, Section, advanced_group, group_section, section_by_slug,
)

ITEM_PREFIX = "item."


def editable_row_paths() -> frozenset[str]:
    """The paths these routes will edit: every list-of-model field, plus the
    slug families, whose entries the Companies page lists and removes beside
    the structured ones. Deliberately NOT every chips field — filters.titles
    and friends have their own inputs on their own pages, and a second way to
    edit them would be a second way to get them wrong."""
    return rows_paths() | {f"sources.{family}" for family in SLUG_SOURCE_FAMILIES}


def _checked(path: str) -> str:
    if path not in editable_row_paths():
        raise HTTPException(status_code=404, detail="not an editable list")
    return path


def _owner_section(path: str) -> Section:
    """The Section (or an AdvancedGroup dressed as one) that renders this
    path — used both to build the redirect target on success and, per Ruling
    R7, to give the standalone row-editing page the same shell (title,
    nav-highlight) as the page it returns to. Derived, never taken from the
    request: an attacker-supplied return URL is a redirect gadget."""
    family = path.removeprefix("sources.")
    if path.startswith("sources.") and family in BOARD_FAMILIES:
        section = section_by_slug("companies")
        assert section is not None  # companies is a permanent section
        return section
    group = advanced_group(path.split(".", 1)[0])
    assert group is not None, f"{path} is editable but owned by no page"
    return group_section(group)


def owner_page(path: str) -> str:
    return f"/settings/{_owner_section(path).slug}"


def decode_row(path: str, form: Mapping[str, list[str]]) -> dict[str, Any]:
    """`item.<field>` form keys -> item-relative values, through the same
    decode rules a section uses (so an unchecked box is False, not missing)."""
    specs = item_fields(path)
    stripped = {
        key[len(ITEM_PREFIX):]: values
        for key, values in form.items() if key.startswith(ITEM_PREFIX)
    }
    values = decode(specs, stripped)
    # _value() (src/web/settings/forms.py) strips whitespace from a text
    # value before this function ever sees it, so a required field left
    # blank or filled with only spaces arrives here as "". None of the row
    # element models declare a non-empty constraint on a plain str field
    # (only rows.py's own scalar/chips case does, for its single synthetic
    # field) — without this check pydantic would accept "" outright and the
    # editor would silently write a blank entry (an extra_queries search with
    # no query, a Workday tenant of ""). Reject it here, at item-relative
    # field granularity, the same way rows.py rejects a blank scalar row.
    blank = [
        spec.path for spec in specs
        if spec.kind == KIND_TEXT and not spec.optional and values.get(spec.path) == ""
    ]
    if blank:
        raise SettingsInvalid([{"loc": p, "msg": "must not be empty"} for p in blank])
    return values


def row_form_ctx(
    request: Request, path: str, *, digest: str | None = None,
    values: Mapping[str, Any] | None = None, errors: Mapping[str, str] | None = None,
    form_errors: list[str] | None = None, gone: bool = False,
) -> dict:
    """The context the standalone row-form page renders from — a superset of
    what settings_base.html's shell itself needs (sections/section/saved), so
    the page shares that shell's nav and title rather than growing its own."""
    return {
        "sections": SECTIONS,
        "section": _owner_section(path),
        "saved": False,
        "path": path,
        "digest": digest,
        "fields": item_fields(path),
        "values": dict(values or {}),
        "errors": dict(errors or {}),
        "form_errors": list(form_errors or []),
        "gone": gone,
        "action": f"/settings/rows/{path}" + (f"/{digest}" if digest else ""),
    }


def _render_row_page(request: Request, *, status_code: int = 200, **ctx) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request, "row_form.html", ctx, status_code=status_code,
    )


def _render_remove_confirm(request: Request, path: str, digest: str, row) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request, "row_remove.html",
        {
            "sections": SECTIONS,
            "section": _owner_section(path),
            "saved": False,
            "path": path,
            "digest": digest,
            "row": row,
            "form_errors": [],
        },
    )


def _gone(request: Request, path: str) -> HTMLResponse:
    return _render_row_page(
        request, status_code=409,
        **row_form_ctx(request, path, gone=True, form_errors=[
            "That entry is no longer configured — someone else "
            "changed these settings. Reload the page.",
        ]),
    )


def register_row_routes(app: FastAPI) -> None:
    @app.get("/settings/rows/{path}/new", response_class=HTMLResponse)
    def row_new(request: Request, path: str):
        return _render_row_page(request, **row_form_ctx(request, _checked(path)))

    @app.get("/settings/rows/{path}/{digest}/edit", response_class=HTMLResponse)
    def row_edit(request: Request, path: str, digest: str):
        _checked(path)
        try:
            row = find_row(request.state.snapshot.cfg, path, digest)
        except RowGone:
            return _gone(request, path)
        return _render_row_page(
            request, **row_form_ctx(request, path, digest=digest, values=row.values)
        )

    @app.post("/settings/rows/{path}", response_class=HTMLResponse)
    async def row_add(request: Request, path: str):
        return await _write(request, _checked(path), digest=None)

    @app.post("/settings/rows/{path}/{digest}", response_class=HTMLResponse)
    async def row_update(request: Request, path: str, digest: str):
        return await _write(request, _checked(path), digest=digest)

    @app.get("/settings/rows/{path}/{digest}/remove", response_class=HTMLResponse)
    def row_remove_confirm(request: Request, path: str, digest: str):
        """Ruling R8: a GET confirm PAGE, not an inline form or a
        formaction button — see the module docstring."""
        _checked(path)
        try:
            row = find_row(request.state.snapshot.cfg, path, digest)
        except RowGone:
            return _gone(request, path)
        return _render_remove_confirm(request, path, digest, row)

    @app.post("/settings/rows/{path}/{digest}/remove", response_class=HTMLResponse)
    async def row_remove(request: Request, path: str, digest: str):
        _checked(path)
        cfg = request.state.snapshot.cfg
        try:
            row = find_row(cfg, path, digest)
            patch = remove_row_patch(cfg, path, digest)
        except RowGone:
            return _gone(request, path)
        note = f"ui: removed a {path.rsplit('.', 1)[-1]} entry"
        outcome = _apply(request, path, digest, row.values, patch, note)
        if outcome is not None:
            return outcome
        return RedirectResponse(owner_page(path) + "?removed=1", status_code=303)


async def _write(request: Request, path: str, *, digest: str | None) -> HTMLResponse:
    form = await request.form()
    raw = {key: form.getlist(key) for key in form.keys()}
    cfg = request.state.snapshot.cfg

    def invalid(exc: SettingsInvalid) -> HTMLResponse:
        # Both error sources — decode() and rows._as_entry() — use
        # item-relative locs ("flavor"), so `known` is the bare field names
        # and the template indexes errors[spec.path]. Getting this prefix
        # wrong in either direction silently drops every field error into
        # the form-level banner instead of under its input.
        by_path, form_level = errors_by_path(
            exc, known={s.path for s in item_fields(path)}
        )
        submitted = {
            k[len(ITEM_PREFIX):]: v[0]
            for k, v in raw.items() if k.startswith(ITEM_PREFIX) and v
        }
        return _render_row_page(request, **row_form_ctx(
            request, path, digest=digest, values=submitted,
            errors=by_path, form_errors=form_level,
        ))

    try:
        values = decode_row(path, raw)
    except SettingsInvalid as exc:
        return invalid(exc)
    try:
        patch = (add_row_patch(cfg, path, values) if digest is None
                 else update_row_patch(cfg, path, digest, values))
    except RowGone:
        return _gone(request, path)
    except SettingsInvalid as exc:
        return invalid(exc)

    verb = "added" if digest is None else "changed"
    note = f"ui: {verb} a {path.rsplit('.', 1)[-1]} entry"
    outcome = _apply(request, path, digest, values, patch, note)
    if outcome is not None:
        return outcome
    return RedirectResponse(owner_page(path) + f"?{verb}=1", status_code=303)


def _apply(
    request: Request, path: str, digest: str | None, values: Mapping[str, Any] | None,
    patch: dict, note: str,
) -> HTMLResponse | None:
    """Write the patch. None means it succeeded.

    ``values`` is what the standalone page re-renders with on a failure here
    — the just-validated submission for an add/update, or the row's own
    stored values for a remove — so a write that fails after validation
    already passed (a model-level rule, or a StaleWrite race) doesn't also
    blank out a form that had nothing wrong with it. This mirrors
    save_section's render_error, which always re-renders with
    submitted=raw on every one of its failure paths, including StaleWrite."""
    def mutate(doc: dict) -> str | None:
        return note if apply_patch(doc, patch) else None

    try:
        request.app.state.service.update_settings(mutate, source="ui")
    except SettingsInvalid as exc:
        # A model-level validator (not a field one) rejected the whole
        # document; no single input owns it, so it goes in the banner.
        _, form_level = errors_by_path(exc, known=set())
        return _render_row_page(request, **row_form_ctx(
            request, path, digest=digest, values=values, form_errors=form_level
        ))
    except StaleWrite:
        return _render_row_page(request, **row_form_ctx(
            request, path, digest=digest, values=values, form_errors=[
                "Someone else saved while you were editing. Reload the page and "
                "reapply your change.",
            ],
        ))
    except NotConfigured:
        return RedirectResponse("/setup", status_code=303)
    return None
