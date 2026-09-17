"""The settings pages.

Route order matters: /settings/advanced is declared before /settings/{slug},
because Starlette matches in registration order and the parameterised route
would otherwise swallow it."""
from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from src.settings.fields import value_at
from src.settings.help import field_help, group_intro
from src.web.settings.readiness import check
from src.web.settings.sections import SECTIONS, section_by_slug, section_fields


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
    }
    ctx.update(extra)
    return ctx


def _render(request: Request, section, **extra) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request, section.template, page_ctx(request, section, **extra)
    )


def register_settings_routes(app: FastAPI) -> None:
    env = app.state.templates.env
    env.globals["field_help"] = field_help
    env.globals["group_intro"] = group_intro
    env.globals["value_at"] = value_at

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
