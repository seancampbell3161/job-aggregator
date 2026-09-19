"""The wizard's pages.

Shape follows src/web/settings/rows.py (Ruling R7/R8): one standalone page per
step, a 303 between steps, and a re-render with the submitted values on
failure. NOT HTMX partials — the settings shell wraps fields in an outer
<form>, and browsers silently drop a nested one. HTMX appears only where it
already does: probe results and the preview poll."""
from __future__ import annotations

import asyncio

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from src.config import SLUG_SOURCE_FAMILIES
from src.resume_intake.draft import DraftFailed, draft_profile_and_filters
from src.resume_intake.extract import ExtractionFailed, extract_text
from src.resume_intake.interview import INTERVIEW_FIELDS, answers_to_patch, decode_answers
from src.settings.documents import validate_document
from src.settings.errors import NotConfigured, SettingsInvalid, StaleWrite
from src.settings.fields import field_map, value_at
from src.settings.service import canonical_doc
from src.web.settings.backup import MAX_UPLOAD_BYTES
from src.web.settings.forms import apply_patch, decode, decode_secrets, errors_by_path
from src.web.settings.probes import ProbeResult, probe_discord, probe_llm, probe_ntfy
from src.web.settings.routes import shown
from src.web.settings.sections import section_by_slug
from src.web.settings.shell import secret_rows
from src.web.wizard.ntfy_topic import suggest_topic, topic_qr_svg
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

LLM_PATHS = (
    "relevance.enabled", "relevance.provider", "relevance.model",
    "relevance.ollama_host", "relevance.timeout_seconds",
)
LLM_SECRETS = ("anthropic_api_key", "google_api_key", "ollama_api_key")

FILTER_PATHS = tuple(section_by_slug("filters").paths)

NOTIFY_SECRETS = ("ntfy_topic_url", "discord_webhook_url")
# {probe} URL slug -> (secret it tests, probe to run). Deliberately its own
# registry rather than reusing settings' _SINK_PROBES: that dict maps the
# same secrets to the same two probe_* functions, but the wizard also tests
# ops_ntfy_topic_url/ops_discord_webhook_url there, which this step never
# shows. Each value is a lambda, not the bare function, so it looks
# `probe_ntfy`/`probe_discord` up on this module fresh at call time — what
# lets tests monkeypatch src.web.wizard.routes.probe_ntfy and have it take
# effect here, same trick settings/routes.py uses for the same reason.
_WIZARD_SINK_PROBES = {
    "ntfy": ("ntfy_topic_url", lambda url: probe_ntfy(url)),
    "discord": ("discord_webhook_url", lambda url: probe_discord(url)),
}


def _wizard_probe_targets() -> dict[str, str]:
    """Secret name -> the {probe} slug it tests, derived fresh from
    _WIZARD_SINK_PROBES on every call so a test that monkeypatches it after
    the app is built still sees the button appear (mirrors settings/routes.py's
    _probe_targets)."""
    return {secret: slug for slug, (secret, _fn) in _WIZARD_SINK_PROBES.items()}


# A step's editable paths and secrets, for wizard_ctx's fields/secrets and for
# save_step. A step with neither (nothing to render field/secret macros for
# yet) falls back to the empty default below.
STEP_FIELDS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "llm": (LLM_PATHS, LLM_SECRETS),
    "review": (FILTER_PATHS, ()),
    "notifications": ((), NOTIFY_SECRETS),
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


def wizard_ctx(request: Request, step, *, paths: tuple[str, ...] = (),
               secrets: tuple[str, ...] = (), **extra) -> dict:
    ctx = {
        "step": step,
        "steps": step_states(current_context(request), request.app.state.stores.wizard.skipped()),
        "cfg": request.state.snapshot.cfg,
        "fields": tuple(field_map()[p] for p in paths),
        "secrets": secret_rows(request.app.state.service, secrets),
        "errors": {},
        "form_errors": [],
        "submitted": None,
        "saved": False,
    }
    ctx.update(extra)
    return ctx


def companies_extra(request: Request) -> dict:
    """Slug-family boards already configured, for the companies step's
    "Already configured" nudge. Only SLUG_SOURCE_FAMILIES (src/config.py) --
    each entry there is a bare slug string. Structured families (Workday,
    Oracle Cloud, ...) still count toward the step's own completion check
    (steps.py's _companies_done reads STRUCTURED_FAMILIES too) but aren't
    enumerated here: their entries are structured boards, not slugs, so
    "family: slug" isn't a sensible display for them, and the "Full companies
    page" link is where they're actually managed."""
    cfg = request.state.snapshot.cfg
    return {
        "configured": [
            (family, slug)
            for family in SLUG_SOURCE_FAMILIES
            for slug in getattr(cfg.sources, family, ()) or ()
        ]
    }


def resume_extra(request: Request) -> dict:
    """The résumé step's own context: the interview question list (fixed,
    never per-request) and whatever answers were last stored for it — either
    from an earlier visit to this step, or just-written by wizard_resume_save
    before re-rendering on a failed extraction."""
    return {
        "interview_fields": INTERVIEW_FIELDS,
        "answers": request.app.state.stores.wizard.get("answers") or {},
    }


def notifications_extra(request: Request) -> dict:
    """The notifications step's own context: probe_targets (for
    secret_field's Test buttons, same shape settings pages use) always, and a
    freshly generated suggested_topic/suggested_qr ONLY when ntfy_topic_url is
    currently unset.

    That gate matters: suggest_topic() is random, and re-suggesting on every
    render would silently swap out a topic the user may already have scanned
    into the ntfy app on their phone. Once the topic is saved,
    service.secret_source(...) reads "stored" (or "env"), the gate closes,
    and the template falls back to secret_field's ordinary stored-source
    notice instead of a suggestion. suggest_topic() is called at most once
    here, and its result is reused for both the pre-filled input and the QR
    (topic_qr_svg(topic)) — never called twice, so the two can never
    disagree."""
    extra: dict = {"probe_targets": _wizard_probe_targets()}
    if request.app.state.service.secret_source("ntfy_topic_url") == "unset":
        topic = suggest_topic()
        extra["suggested_topic"] = topic
        extra["suggested_qr"] = topic_qr_svg(topic)
    return extra


def review_prefill(request: Request) -> dict:
    """What the filter inputs start out showing.

    Once a profile document has actually been saved, this page is an ordinary
    settings page from then on: the config is truth, so no prefill (an empty
    mapping makes review_shown() fall straight through to the config). That
    matters for a direct revisit after approval — without it, a stale
    pre-edit draft cached from before that save would keep outranking the
    config forever, so an intentional edit made at approval time would look
    like it silently reverted on the next visit.

    Before that first save, the draft wins when there is one: it was built
    FROM the interview answers, so it is the later and better-informed word.
    Without a draft — no LLM, or a failed call — fall back to the answers
    themselves, which already bind to real config paths. Otherwise the eight
    questions the user just answered would buy them an empty form."""
    if request.state.snapshot.documents.profile:
        return {}
    store = request.app.state.stores.wizard
    draft = store.get("draft") or {}
    if draft.get("status") == "ok":
        return dict(draft.get("filters") or {})
    return answers_to_patch(store.get("answers") or {})


def review_shown(ctx_submitted, cfg, spec, prefill):
    """review's own shown(): the same submitted-or-config rule settings pages
    use (settings.routes.shown), with one more layer underneath. A not-yet-
    saved draft or interview prefill wins over the stored config, so a first
    visit shows the draft rather than empty/default fields. Once the user has
    actually typed something (ctx_submitted is not None, i.e. a re-render
    after a failed save), that submitted value always wins, exactly like
    shown() — this never overrides what's on screen with the draft."""
    if ctx_submitted is None and spec.path in prefill:
        return prefill[spec.path]
    return shown(ctx_submitted, cfg, spec)


def review_extra(request: Request) -> dict:
    """The review step's own context: the prefill mapping described above, and
    the profile document itself when one is already saved — so the profile
    textarea (which has no config path to read a "current value" from the way
    the filter fields do) also shows what was actually saved rather than a
    stale draft, on a revisit after approval."""
    return {
        "prefill": review_prefill(request),
        "saved_profile": request.state.snapshot.documents.profile,
    }


async def _ensure_draft(request: Request) -> tuple[dict | None, str | None]:
    """(draft dict, error message). Generated on first visit and cached in
    wizard_ui, so a reload does not pay for another LLM round trip. A failure
    is cached too — as an error record — so a broken provider does not get
    re-called on every render.

    Once a profile document is saved, this step is done and behaves like any
    other settings page: no more auto-drafting (nothing left to bootstrap),
    and "Draft again" becomes a no-op rather than silently overwriting a
    hand-made edit with a regenerated suggestion nobody asked to redo."""
    snap = request.state.snapshot
    if snap.documents.profile:
        return None, None

    store = request.app.state.stores.wizard
    cached = store.get("draft")
    if cached is not None:
        return (cached, None) if cached.get("status") == "ok" else (None, cached.get("error"))

    resume_text = snap.documents.resume_text
    if not resume_text:
        return None, None  # nothing to draft from; plain forms, no error shown

    answers = store.get("answers") or {}
    try:
        draft = await draft_profile_and_filters(
            snap.cfg, resume_text=resume_text, answers=answers,
        )
    except DraftFailed as exc:
        store.put("draft", {"status": "error", "error": str(exc)})
        return None, str(exc)

    record = {
        "status": "ok", "profile_md": draft.profile_md,
        "filters": draft.filters, "warnings": draft.warnings,
    }
    store.put("draft", record)
    return record, None


def render_step(request: Request, step, **extra) -> HTMLResponse:
    paths, secrets = STEP_FIELDS.get(step.slug, ((), ()))
    if step.slug == "companies":
        extra = {**companies_extra(request), **extra}
    elif step.slug == "notifications":
        extra = {**notifications_extra(request), **extra}
    elif step.slug == "resume":
        extra = {**resume_extra(request), **extra}
    elif step.slug == "review":
        extra = {**review_extra(request), **extra}
    elif step.slug == "preview":
        extra = {"preview": request.app.state.stores.wizard.get("preview"), **extra}
    return request.app.state.templates.TemplateResponse(
        request, TEMPLATES[step.slug],
        wizard_ctx(request, step, paths=paths, secrets=secrets, **extra)
    )


def _probe_partial(request: Request, result: ProbeResult) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request, "_probe_result.html", {"result": result}
    )


def save_step(request: Request, step, paths, secrets, form_raw) -> HTMLResponse | RedirectResponse:
    """Decode a step's form into a dotted-path patch and write it with
    source="wizard". Mirrors settings' save_section: filter the patch against
    the config in effect so an unchanged step writes no version, write secrets
    only after the settings patch succeeds, and re-render with the submitted
    values on any failure so nothing typed is lost."""
    service = request.app.state.service
    specs = tuple(field_map()[p] for p in paths)

    def render_error(errors, form_errors):
        return render_step(request, step, errors=errors, form_errors=form_errors,
                            submitted=form_raw)

    try:
        patch = decode(specs, form_raw)
    except SettingsInvalid as exc:
        by_path, form_level = errors_by_path(exc, known={s.path for s in specs})
        return render_error(by_path, form_level)

    cfg = request.state.snapshot.cfg
    patch = {p: v for p, v in patch.items() if v != value_at(cfg, p)}

    def mutate(doc: dict) -> str | None:
        return f"wizard: {step.title}" if apply_patch(doc, patch) else None

    try:
        service.update_settings(mutate, source="wizard")
    except SettingsInvalid as exc:
        by_path, form_level = errors_by_path(exc, known={s.path for s in specs})
        return render_error(by_path, form_level)
    except StaleWrite:
        return render_error({}, [
            "Someone else saved while you were on this step. Reload and reapply."
        ])
    except NotConfigured:
        return RedirectResponse("/setup", status_code=303)

    if secrets:
        to_set, to_clear = decode_secrets(secrets, form_raw, service.secret_source)
        for name, value in to_set.items():
            service.set_secret(name, value)
        for name in to_clear:
            service.clear_secret(name)

    return RedirectResponse("/wizard", status_code=303)


def register_wizard_routes(app: FastAPI) -> None:
    app.state.templates.env.globals["review_shown"] = review_shown

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

    @app.post("/wizard/llm")
    async def wizard_llm_save(request: Request):
        form = await request.form()
        raw = {k: form.getlist(k) for k in form.keys()}
        return save_step(request, step_by_slug("llm"), LLM_PATHS, LLM_SECRETS, raw)

    @app.post("/wizard/llm/test")
    async def wizard_llm_test(request: Request):
        """Probe the UNSAVED form values, exactly as /settings/llm/test/llm
        does — a Test writes nothing."""
        form = await request.form()
        raw = {k: form.getlist(k) for k in form.keys()}
        service = request.app.state.service
        snap = request.state.snapshot
        specs = tuple(field_map()[p] for p in LLM_PATHS)
        doc = canonical_doc(snap.cfg)
        try:
            apply_patch(doc, decode(specs, raw))
            cfg = service.parse_settings(doc)
        except SettingsInvalid as exc:
            return _probe_partial(request, ProbeResult(False, "; ".join(
                f"{e.get('loc', '')}: {e['msg']}" for e in exc.errors)))
        typed, to_clear = decode_secrets(LLM_SECRETS, raw, service.secret_source)
        merged = {name: service.effective_secret(name) for name in LLM_SECRETS}
        merged.update(typed)
        # A pending "clear" checkbox is what Save will actually do to this
        # secret — the probe must reflect that too, or ticking clear and
        # pressing Test reports green off the still-stored value.
        for name in to_clear:
            merged[name] = ""
        cfg = cfg.model_copy(update={"secrets": cfg.secrets.model_copy(update=merged)})
        return _probe_partial(request, await probe_llm(cfg, snap.documents.profile))

    @app.post("/wizard/resume")
    async def wizard_resume_save(request: Request):
        step = step_by_slug("resume")
        form = await request.form()
        raw = {k: form.getlist(k) for k in form.keys()}
        answers = decode_answers(raw)

        upload = form.get("upload")
        pasted = (form.get("pasted") or "").strip()
        try:
            if upload is not None and getattr(upload, "filename", ""):
                data = await upload.read()
                if len(data) > MAX_UPLOAD_BYTES:
                    raise ExtractionFailed("That file is too large.")
                text = extract_text(data, upload.filename)
            elif pasted:
                text = extract_text(pasted.encode("utf-8"), "pasted.txt")
            else:
                raise ExtractionFailed(
                    "Upload a résumé or paste its text to continue — or skip "
                    "this step and fill the next one in by hand."
                )
        except ExtractionFailed as exc:
            # Remember the answers even though the résumé failed, so a retry
            # does not re-ask all eight questions.
            request.app.state.stores.wizard.put("answers", answers)
            return render_step(request, step, form_errors=[str(exc)], submitted=raw)

        request.app.state.service.save_document("resume_text", text, source="wizard")
        request.app.state.stores.wizard.put("answers", answers)
        # A new résumé invalidates any draft made from the previous one.
        request.app.state.stores.wizard.delete("draft")
        return RedirectResponse("/wizard", status_code=303)

    @app.post("/wizard/notifications")
    async def wizard_notifications_save(request: Request):
        form = await request.form()
        raw = {k: form.getlist(k) for k in form.keys()}
        return save_step(request, step_by_slug("notifications"), (), NOTIFY_SECRETS, raw)

    @app.post("/wizard/notifications/test/{probe}")
    async def wizard_notifications_test(request: Request, probe: str):
        entry = _WIZARD_SINK_PROBES.get(probe)
        if entry is None:
            raise HTTPException(status_code=404)
        secret_name, run = entry
        form = await request.form()
        value = (form.get(f"secret.{secret_name}") or "").strip()
        # A pending "clear" checkbox is what Save will actually do to this
        # secret — the probe must reflect that too, or ticking clear and
        # pressing Test reports green off the still-stored value (the same
        # trap fixed for /wizard/llm/test in Task 7). Only fall back to the
        # stored value when the field is blank AND nothing is telling Save to
        # delete it.
        if not value and not form.get(f"clear.{secret_name}"):
            value = request.app.state.service.effective_secret(secret_name)
        if not value:
            return _probe_partial(request, ProbeResult(False, "Nothing to test yet."))
        return _probe_partial(request, await run(value))

    @app.get("/wizard/review", response_class=HTMLResponse)
    async def wizard_review(request: Request):
        draft, error = await _ensure_draft(request)
        return render_step(
            request, step_by_slug("review"), draft=draft,
            form_errors=[error] if error else [],
        )

    @app.post("/wizard/review/redraft")
    async def wizard_redraft(request: Request):
        request.app.state.stores.wizard.delete("draft")
        return RedirectResponse("/wizard/review", status_code=303)

    @app.post("/wizard/review")
    async def wizard_review_save(request: Request):
        step = step_by_slug("review")
        form = await request.form()
        raw = {k: form.getlist(k) for k in form.keys()}
        service = request.app.state.service
        specs = tuple(field_map()[p] for p in FILTER_PATHS)
        profile = (form.get("profile") or "").strip()

        def render_error(errors, form_errors):
            return render_step(request, step, errors=errors, form_errors=form_errors,
                               submitted=raw,
                               draft=request.app.state.stores.wizard.get("draft"))

        try:
            patch = decode(specs, raw)
            validate_document("profile", profile)
        except SettingsInvalid as exc:
            by_path, form_level = errors_by_path(exc, known={s.path for s in specs})
            return render_error(by_path, form_level)

        doc = canonical_doc(request.state.snapshot.cfg)
        apply_patch(doc, patch)
        source = "llm_draft" if (request.app.state.stores.wizard.get("draft") or {}
                                 ).get("status") == "ok" else "wizard"
        try:
            # One bundle: settings and the profile document validate together,
            # so a bad filter and a bad profile surface in the same pass.
            service.save_bundle(doc, {"profile": profile}, source=source,
                                note="wizard: profile and filters")
        except SettingsInvalid as exc:
            by_path, form_level = errors_by_path(exc, known={s.path for s in specs})
            return render_error(by_path, form_level)
        except StaleWrite:
            return render_error({}, ["Someone else saved while you were editing. "
                                     "Reload and reapply."])
        # The draft's job was to prefill this form once; now that a profile
        # document exists, review_prefill()/_ensure_draft() stop consulting it
        # on their own (the config is the current truth from here on) — this
        # delete is just hygiene, so a stale draft blob doesn't sit in
        # wizard_ui forever.
        request.app.state.stores.wizard.delete("draft")
        return RedirectResponse("/wizard", status_code=303)

    @app.post("/wizard/preview/start")
    async def wizard_preview_start(request: Request):
        from src.web.wizard.preview import run_preview
        # Written synchronously, before the background task is even
        # scheduled, so the very next request (the page's first poll, or a
        # test asserting on it) is guaranteed to see "running" rather than
        # racing the event loop for a chance to run the task's first line.
        request.app.state.stores.wizard.put("preview", {"status": "running"})
        # Fire and forget: the page polls /wizard/preview/status. Keep a
        # reference so the task is not garbage-collected mid-run.
        task = asyncio.create_task(run_preview(request.app))
        request.app.state.preview_task = task
        return RedirectResponse("/wizard/preview", status_code=303)

    @app.get("/wizard/preview/status", response_class=HTMLResponse)
    def wizard_preview_status(request: Request):
        return request.app.state.templates.TemplateResponse(
            request, "_wizard_preview_result.html",
            {"preview": request.app.state.stores.wizard.get("preview")},
        )

    # Declared before /wizard/{slug} — Starlette matches in registration
    # order, and the parameterised route would otherwise swallow /wizard/done
    # (slug="done"), the same ordering constraint documented at the top of
    # src/web/settings/routes.py. /wizard/review and its POST siblings above
    # are declared here for the same reason: /wizard/{slug} would otherwise
    # swallow GET /wizard/review too. The two preview routes just above don't
    # actually need this: {slug} is a single path segment (no "/"), so it can
    # never match "preview/start" or "preview/status" regardless of order —
    # they're grouped here for readability, not correctness.
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
