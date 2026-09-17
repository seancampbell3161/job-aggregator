"""The settings pages.

Route order matters: /settings/advanced is declared before /settings/{slug},
because Starlette matches in registration order and the parameterised route
would otherwise swallow it."""
from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from src.settings.errors import NotConfigured, SettingsInvalid, StaleWrite
from src.settings.fields import (
    KIND_BOOL, KIND_CHIPS, KIND_MULTI_CHOICE, KIND_READ_ONLY, optional_groups, value_at,
)
from src.settings.help import field_help, group_intro
from src.web.settings.forms import apply_patch, decode, errors_by_path
from src.web.settings.readiness import check
from src.web.settings.sections import Section, SECTIONS, section_by_slug, section_fields


def page_ctx(request: Request, section, **extra) -> dict:
    """The context every section template expects. Values render from the
    validated AppConfig on the request snapshot, never from the stored doc —
    the doc is sparse (canonical_doc drops defaults), the config is complete."""
    ctx = {
        "sections": SECTIONS,
        "section": section,
        "cfg": request.state.snapshot.cfg,
        "fields": section_fields(section) if section.paths else (),
        "errors": {},
        "form_errors": [],
        "saved": False,
        "submitted": None,
    }
    ctx.update(extra)
    return ctx


def _render(request: Request, section, **extra) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request, section.template, page_ctx(request, section, **extra)
    )


def shown(ctx_submitted, cfg, spec):
    """What an input should display: what the user typed if this render follows
    a failed save, otherwise the stored value."""
    # A read-only field is never submitted, so echoing the form would render it
    # as empty after a failed save. Always read those from the config.
    if ctx_submitted is None or spec.kind == KIND_READ_ONLY:
        return value_at(cfg, spec.path)
    values = ctx_submitted.get(spec.path, [])
    if spec.kind in (KIND_CHIPS, KIND_MULTI_CHOICE):
        return values
    if spec.kind == KIND_BOOL:
        return bool(values)
    return values[0] if values else None


async def save_section(request: Request, section: Section, **extra) -> HTMLResponse:
    """POST for a hand-built section: decode the form into a dotted-path patch,
    apply it through update_settings, re-render either way.

    ``extra`` is forwarded to every render so a page that carries its own
    context (the generated Advanced groups) keeps it across a failed save.

    On any failure the page re-renders with the SUBMITTED values, so nothing
    typed is lost — hence `submitted` in the context."""
    form = await request.form()
    raw = {key: form.getlist(key) for key in form.keys()}
    specs = section_fields(section)
    groups = [g for g in optional_groups() if any(p.startswith(g + ".") for p in section.paths)]

    def render_error(errors: dict, form_errors: list[str]) -> HTMLResponse:
        return request.app.state.templates.TemplateResponse(
            request, section.template,
            page_ctx(request, section, errors=errors, form_errors=form_errors,
                     submitted=raw, **extra),
        )

    try:
        patch = decode(specs, raw, optional_groups=groups)
    except SettingsInvalid as exc:
        by_path, form_level = errors_by_path(exc, known={s.path for s in specs})
        return render_error(by_path, form_level)

    # decode() emits one entry per field, submitted or not (that's what makes
    # an unchecked checkbox work) — so an unchanged resubmission still comes
    # back with a full set of entries. Keep only the ones that actually move
    # a path away from what's in effect now: apply_patch's own "changed" test
    # is structural against the *sparse* stored doc, and a value that merely
    # matches its pydantic default is never present there, so re-asserting it
    # would look like a change even though canonical_doc() strips it right
    # back out on write — spuriously bumping the version on every save. cfg
    # is fixed for this request, so this filtering stays stable across the
    # retry update_settings may run mutate through.
    cfg = request.state.snapshot.cfg
    patch = {path: value for path, value in patch.items() if value != value_at(cfg, path)}

    def mutate(doc: dict) -> str | None:
        return f"ui: {section.title}" if apply_patch(doc, patch) else None

    try:
        request.app.state.service.update_settings(mutate, source="ui")
    except SettingsInvalid as exc:
        by_path, form_level = errors_by_path(exc, known={s.path for s in specs})
        return render_error(by_path, form_level)
    except StaleWrite:
        return render_error({}, [
            "Someone else saved while you were editing. Reload the page and "
            "reapply your change."
        ])
    except NotConfigured:
        return RedirectResponse("/setup", status_code=303)

    # Re-read so the page shows what was actually stored, not what was typed.
    request.state.snapshot = request.app.state.service.snapshot()
    return _render(request, section, saved=True, **extra)


def register_settings_routes(app: FastAPI) -> None:
    env = app.state.templates.env
    env.globals["field_help"] = field_help
    env.globals["group_intro"] = group_intro
    env.globals["value_at"] = value_at
    env.globals["shown"] = shown

    @app.get("/settings", response_class=HTMLResponse)
    def settings_index():
        return RedirectResponse("/settings/overview", status_code=303)

    @app.get("/settings/overview", response_class=HTMLResponse)
    def overview(request: Request):
        snap = request.state.snapshot
        service = request.app.state.service
        warnings = check(
            snap.cfg,
            has_profile=bool(snap.documents.profile),
            secret_source=service.secret_source,
        )
        return _render(
            request, section_by_slug("overview"),
            warnings=warnings, snapshot=snap,
        )

    @app.get("/settings/{slug}", response_class=HTMLResponse)
    def section_page(request: Request, slug: str):
        section = section_by_slug(slug)
        if section is None:
            raise HTTPException(status_code=404)
        return _render(request, section)

    @app.post("/settings/{slug}", response_class=HTMLResponse)
    async def section_save(request: Request, slug: str):
        section = section_by_slug(slug)
        if section is None or not section.paths:
            raise HTTPException(status_code=404)
        return await save_section(request, section)
