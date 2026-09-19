"""The wizard's pages.

Shape follows src/web/settings/rows.py (Ruling R7/R8): one standalone page per
step, a 303 between steps, and a re-render with the submitted values on
failure. NOT HTMX partials — the settings shell wraps fields in an outer
<form>, and browsers silently drop a nested one. HTMX appears only where it
already does: probe results and the preview poll."""
from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from src.web.wizard.steps import (
    build_context, next_step, step_by_slug, step_states,
)

# Template per step slug. A step with no bespoke page would 500 on render, so
# every WIZARD_STEPS entry must appear here (test_shell asserts the pairing).
TEMPLATES = {
    "llm": "wizard_llm.html",
    "resume": "wizard_resume.html",
    "review": "wizard_review.html",
    "companies": "wizard_companies.html",
    "notifications": "wizard_notifications.html",
    "preview": "wizard_preview.html",
}


def current_context(request: Request):
    """The step context for this request's snapshot."""
    service = request.app.state.service
    snap = request.state.snapshot
    preview = request.app.state.stores.wizard.get("preview")
    return build_context(
        snap.cfg, snap.documents, service.secret_source,
        preview_done=bool(preview and preview.get("status") in ("ok", "error")),
    )


def wizard_ctx(request: Request, step, **extra) -> dict:
    ctx = {
        "step": step,
        "steps": step_states(current_context(request), request.app.state.stores.wizard.skipped()),
        "cfg": request.state.snapshot.cfg,
        "errors": {},
        "form_errors": [],
        "submitted": None,
        "saved": False,
    }
    ctx.update(extra)
    return ctx


def render_step(request: Request, step, **extra) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request, TEMPLATES[step.slug], wizard_ctx(request, step, **extra)
    )


def register_wizard_routes(app: FastAPI) -> None:
    @app.get("/wizard")
    def wizard_root(request: Request):
        """Always the entry point: recomputes where the user actually is, so a
        bookmarked /wizard resumes rather than restarting."""
        step = next_step(current_context(request), request.app.state.stores.wizard.skipped())
        return RedirectResponse(
            "/wizard/done" if step is None else f"/wizard/{step.slug}", status_code=303
        )

    @app.get("/wizard/done", response_class=HTMLResponse)
    def wizard_done(request: Request):
        return request.app.state.templates.TemplateResponse(
            request, "wizard_done.html", wizard_ctx(request, None)
        )

    # Declared before /wizard/{slug} — Starlette matches in registration
    # order, and the parameterised route would otherwise swallow /wizard/done
    # (slug="done"), the same ordering constraint documented at the top of
    # src/web/settings/routes.py.
    @app.get("/wizard/{slug}", response_class=HTMLResponse)
    def wizard_step(request: Request, slug: str):
        step = step_by_slug(slug)
        if step is None:
            raise HTTPException(status_code=404)
        return render_step(request, step)

    @app.post("/wizard/{slug}/skip")
    def wizard_skip(request: Request, slug: str):
        if step_by_slug(slug) is None:
            raise HTTPException(status_code=404)
        request.app.state.stores.wizard.skip(slug)
        return RedirectResponse("/wizard", status_code=303)
