"""Draft the structured résumé (content.json) from the stored résumé text.

A background task plus a polled status, copied from the wizard's preview
(src/web/wizard/routes.py:581-593) rather than awaited inline: a 16k-token
generation is a minute-plus call, and a blocking GET returns a proxy timeout
with the work already paid for.

The pending draft lives in the non-versioned wizard_ui KV under
"content_draft". That table is per-install UI state by design
(src/state_sqlite.py:1145-1148) — it must stay out of version history, backup
zips, and CONFIG.md's CI-enforced flag table — and this page is reachable
outside the wizard, which makes the table name a mild misnomer. Documenting
that is cheaper than migrating a table over a naming nit.

Registered separately from the /settings/{slug} catch-all for consistency with
companies/history/backup, though this module's paths are three segments deep
and could not be swallowed by it in any case."""
from __future__ import annotations

import asyncio
import json
import logging

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from src.resume_intake.content_draft import NO_RESUME, draft_content
from src.resume_intake.draft import DraftFailed
from src.settings.errors import SettingsInvalid
from src.web.settings.sections import section_by_slug
from src.web.settings.shell import page_ctx

log = logging.getLogger(__name__)

KEY = "content_draft"
PAGE_TEMPLATE = "settings_content_draft.html"
RESULT_TEMPLATE = "_content_draft_result.html"
PATH = "/settings/documents/draft"


async def run_content_draft(app) -> dict:
    """Run the draft and store its record. Never raises: a drafting failure is
    a message on a page. Writes no document — only the review form's save
    does, which is what makes the review a real gate."""
    store = app.state.stores.wizard
    store.put(KEY, {"status": "running"})
    try:
        snap = app.state.service.snapshot()
        draft = await draft_content(snap.cfg, resume_text=snap.documents.resume_text or "")
        record = {"status": "ok", "document": draft.document, "contact": draft.contact}
    except DraftFailed as exc:
        record = {"status": "error", "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 — a failed draft is a message, not a 500
        log.warning("content_draft_task_failed",
                    extra={"error": str(exc), "error_type": type(exc).__name__})
        record = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
    store.put(KEY, record)
    return record


def _render(request: Request, **extra) -> HTMLResponse:
    """The Documents section's nav and chrome, this module's template."""
    return request.app.state.templates.TemplateResponse(
        request, PAGE_TEMPLATE,
        page_ctx(request, section_by_slug("documents"),
                 draft=request.app.state.stores.wizard.get(KEY), **extra),
    )


def register_content_draft_routes(app: FastAPI) -> None:
    @app.get(PATH, response_class=HTMLResponse)
    def content_draft_page(request: Request):
        return _render(request)

    @app.post(PATH)
    async def content_draft_start(request: Request):
        snap = request.state.snapshot
        store = request.app.state.stores.wizard
        form = await request.form()

        if not (snap.documents.resume_text or "").strip():
            store.put(KEY, {"status": "error", "error": NO_RESUME})
            return RedirectResponse(PATH, status_code=303)

        # Refuse by default and name what would be replaced, the same shape
        # the settings import guard uses (src/settings/transfer.py:66-72).
        if snap.documents.resume_content and not form.get("overwrite"):
            store.put(KEY, {"status": "confirm"})
            return RedirectResponse(PATH, status_code=303)

        # Written synchronously, before the task is scheduled, so the page's
        # first poll is guaranteed to see "running" rather than racing the
        # event loop. Same ordering as wizard_preview_start.
        store.put(KEY, {"status": "running"})
        # Keep a reference so the task is not garbage-collected mid-run.
        request.app.state.content_draft_task = asyncio.create_task(
            run_content_draft(request.app))
        return RedirectResponse(PATH, status_code=303)

    @app.get(f"{PATH}/status", response_class=HTMLResponse)
    def content_draft_status(request: Request):
        return request.app.state.templates.TemplateResponse(
            request, RESULT_TEMPLATE,
            {"draft": request.app.state.stores.wizard.get(KEY)},
        )
