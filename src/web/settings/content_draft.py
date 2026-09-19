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
import re
from typing import Any, Mapping

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from src.resume_intake.content_draft import NO_RESUME, draft_content
from src.resume_intake.draft import DraftFailed
from src.resume_intake.facts_scaffold import build_facts_yaml
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


_DIGIT = re.compile(r"\d")


def _one(form: Mapping[str, list[str]], name: str, default: str = "") -> str:
    """The first value for ``name`` in ``form``, or ``default`` if absent.

    ``form`` values are always ``list[str]`` — the save route builds it with
    ``request.form().getlist()`` for every key, and every caller (production
    and tests alike) follows that shape, so there is no plain-string branch
    to support here."""
    values = form.get(name)
    return str(values[0]).strip() if values else default


def apply_edits(document: Mapping[str, Any], form: Mapping[str, list[str]]) -> dict:
    """The stored draft with the user's edits applied.

    Structure comes from the draft, never from the form: the form carries only
    per-id bullet text, per-id drop flags, per-entry strings and the skills
    chips. A malformed or hostile post can therefore change text and remove
    bullets, but cannot invent an entry, resurrect a dropped one, or rewrite
    an id — which is what keeps the ids stable across a review.

    An experience or project left with no bullets after edits (every bullet
    dropped, or edited down to blank text) is dropped from the result
    entirely rather than kept with an empty bullet list. That is what makes
    an empty entry structurally impossible to write: parse_content on its own
    accepts one, so the save route's refusal (nothing left to tailor at all)
    cannot rely on it alone. Dropping every bullet from a role therefore
    means "delete this role," not "keep an empty one" — a projects-only or
    experience-only result is fine as long as something remains."""

    def edited_bullets(bullets):
        out = []
        for bullet in bullets or []:
            bullet_id = bullet["id"]
            if form.get(f"drop.{bullet_id}"):
                continue
            text = _one(form, f"text.{bullet_id}", bullet.get("text", ""))
            if not text:
                continue
            out.append({**bullet, "text": text,
                        "metric_bearing": bool(_DIGIT.search(text))})
        return out

    experiences = [
        {**e,
         "company": _one(form, f"company.{e['id']}", e.get("company", "")),
         "role": _one(form, f"role.{e['id']}", e.get("role", "")),
         "start": _one(form, f"start.{e['id']}", e.get("start", "")),
         "end": _one(form, f"end.{e['id']}", e.get("end", "")),
         "bullets": edited_bullets(e.get("bullets"))}
        for e in document.get("experiences") or []
    ]
    experiences = [e for e in experiences if e["bullets"]]
    projects = [
        {**p,
         "name": _one(form, f"name.{p['id']}", p.get("name", "")),
         "subtitle": _one(form, f"subtitle.{p['id']}", p.get("subtitle", "")),
         "dates": _one(form, f"dates.{p['id']}", p.get("dates", "")),
         "bullets": edited_bullets(p.get("bullets"))}
        for p in document.get("projects") or []
    ]
    projects = [p for p in projects if p["bullets"]]

    # Chips carry names only, so a kept skill's category is looked up from the
    # draft and a newly typed one gets an empty category — which parse_content
    # accepts (src/tailor/content.py:45).
    names = list(dict.fromkeys(
        n.strip() for n in (form.get("skills") or []) if n and n.strip()))
    by_name = {str(s.get("name", "")).strip().lower(): s
               for s in document.get("skills") or []}
    skills = [
        {"name": name,
         "category": str(by_name.get(name.lower(), {}).get("category", "")),
         "tags": list(by_name.get(name.lower(), {}).get("tags", []))}
        for name in names
    ]

    return {**document, "skills": skills, "experiences": experiences, "projects": projects}


def _render(request: Request, *, draft: dict | None = None, **extra) -> HTMLResponse:
    """The Documents section's nav and chrome, this module's template.

    ``draft`` overrides the stored record when given — the save route passes
    the record with its ``document`` swapped for apply_edits's output when
    re-rendering after a refusal, so the user's typed text and drop choices
    survive the redisplay instead of reverting to the last stored draft."""
    return request.app.state.templates.TemplateResponse(
        request, PAGE_TEMPLATE,
        page_ctx(request, section_by_slug("documents"),
                 draft=request.app.state.stores.wizard.get(KEY) if draft is None else draft,
                 **extra),
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

    @app.post(f"{PATH}/save", response_class=HTMLResponse)
    async def content_draft_save(request: Request):
        store = request.app.state.stores.wizard
        record = store.get(KEY) or {}
        if record.get("status") != "ok":
            raise HTTPException(status_code=409, detail="no draft to save")

        form = await request.form()
        raw = {k: form.getlist(k) for k in form.keys()}
        document = apply_edits(record["document"], raw)
        service = request.app.state.service
        # Used below for the SettingsInvalid re-render: the user's typed
        # text is valid input the validator rejected for some other reason,
        # and it must survive the redisplay rather than silently reverting.
        edited_record = {**record, "document": document}

        # apply_edits already drops any entry left with no bullets, so an
        # empty entry can never be written; refuse only once nothing (of
        # either kind) is left to save. A projects-only or experience-only
        # result is fine — that's a real résumé, not an empty one.
        if not document.get("experiences") and not document.get("projects"):
            # Re-render from the STORED record here, not edited_record: the
            # edited document has zero entries by construction (that's what
            # triggered this refusal), so rendering it would show a form
            # with no bullet fields at all — nothing the user could act on
            # to fix it. There is no salvageable edit to preserve, since the
            # dropped bullets are themselves the thing being refused;
            # restoring the stored draft brings every bullet back so the
            # user can choose differently in place.
            return _render(request, draft=record, form_errors=[
                "Every bullet was dropped or blank, so there would be nothing "
                "to tailor. Keep at least one, or edit the document as JSON."])

        try:
            service.save_document(
                "resume_content",
                json.dumps(document, indent=2, ensure_ascii=False),
                source="llm_draft",
            )
        except SettingsInvalid as exc:
            return _render(request, draft=edited_record, form_errors=[str(exc)])

        snap = service.snapshot()
        request.state.snapshot = snap
        # Only when there is nothing there: an existing apply kit is the
        # user's own work and this scaffold is a starting point, not an
        # improvement on it.
        if not snap.documents.kit_facts:
            try:
                service.save_document(
                    "kit_facts",
                    build_facts_yaml(record.get("contact") or {}, snap.cfg),
                    source="llm_draft",
                )
            except Exception as exc:  # noqa: BLE001 — the résumé data just
                # written above is what matters; build_facts_yaml's output
                # should always validate, but a failure here must not turn
                # an otherwise-successful save into a 500 after the write
                # already happened. The user can write facts.yaml by hand.
                log.warning("kit_facts_scaffold_failed",
                            extra={"error": str(exc), "error_type": type(exc).__name__})

        store.delete(KEY)
        return RedirectResponse("/settings/documents?kind=resume_content",
                                status_code=303)
