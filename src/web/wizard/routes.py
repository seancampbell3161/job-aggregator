"""The wizard's pages.

Shape follows src/web/settings/rows.py (Ruling R7/R8): one standalone page per
step, a 303 between steps, and a re-render with the submitted values on
failure. NOT HTMX partials — the settings shell wraps fields in an outer
<form>, and browsers silently drop a nested one. HTMX appears only where it
already does: probe results and the preview poll."""
from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from src.settings.errors import NotConfigured, SettingsInvalid, StaleWrite
from src.settings.fields import field_map, value_at
from src.settings.service import canonical_doc
from src.web.settings.forms import apply_patch, decode, decode_secrets, errors_by_path
from src.web.settings.probes import ProbeResult, probe_llm
from src.web.settings.shell import secret_rows
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

# A step's editable paths and secrets, for wizard_ctx's fields/secrets and for
# save_step. A step with neither (nothing to render field/secret macros for
# yet) falls back to the empty default below.
STEP_FIELDS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "llm": (LLM_PATHS, LLM_SECRETS),
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


def render_step(request: Request, step, **extra) -> HTMLResponse:
    paths, secrets = STEP_FIELDS.get(step.slug, ((), ()))
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
        typed, _ = decode_secrets(LLM_SECRETS, raw, service.secret_source)
        merged = {name: service.effective_secret(name) for name in LLM_SECRETS}
        merged.update(typed)
        cfg = cfg.model_copy(update={"secrets": cfg.secrets.model_copy(update=merged)})
        return _probe_partial(request, await probe_llm(cfg, snap.documents.profile))

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
