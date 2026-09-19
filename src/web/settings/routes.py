"""The settings pages.

Route order matters: /settings/advanced is declared before /settings/{slug},
because Starlette matches in registration order and the parameterised route
would otherwise swallow it. register_row_routes(app) is called first, ahead
of every route this module declares, per Ruling R1 — today's /settings/rows/...
routes are all 3+ segments, so they cannot actually collide with the
single-segment /settings/{slug} catch-all (verified by moving the call and
rerunning the suite: nothing broke), but Tasks 11 and 13 add single-segment
paths under this same registration, where the collision is real.

register_companies_routes(app), register_history_routes(app), and
register_backup_routes(app) are also called early, right after
register_row_routes, for that exact reason: /settings/companies,
/settings/history, and /settings/backup are each a single path segment, so
unlike the row routes they would genuinely be swallowed by /settings/{slug}
if that catch-all were declared first. register_content_draft_routes(app) is
registered alongside them for consistency, though its own paths
(/settings/documents/draft and .../draft/status) are three segments deep and
could not be swallowed by the catch-all regardless of ordering.

register_content_draft_routes was imported lazily here for a while, because
src/web/settings/content_draft.py reaches src.resume_intake.content_draft,
that module used to reach src.resume_intake.draft for DraftFailed, and
draft.py imports src.web.settings.forms — which, entered from
src.resume_intake.draft, ran this package's own __init__ (`from
src.web.settings.routes import register_settings_routes`) and closed the loop
back onto a half-imported draft.py. Moving DraftFailed to the leaf module
src/resume_intake/errors.py cut the return leg, so this is an ordinary
module-level import again; tests/resume_intake/test_content_draft.py asserts
in a fresh interpreter that the drafter pulls in no src.web module at all.
draft.py's own reach into src.web.settings.forms is still an inversion, and
still the larger cleanup (moving apply_patch and the section registry out of
the web package) that has not been done.

page_ctx/render_section/secret_rows live in shell.py (Ruling R10) — this
module still uses them constantly, but so does every leaf settings route
module, and none of them (including this one, now) reaches into another for
its own private helpers."""
from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from src.config import Secrets
from src.settings.documents import DOCUMENT_KINDS
from src.settings.errors import NotConfigured, SettingsInvalid, StaleWrite
from src.settings.fields import (
    KIND_BOOL, KIND_CHIPS, KIND_MULTI_CHOICE, KIND_ROWS, NON_FORM_KINDS, optional_groups,
    value_at,
)
from src.settings.help import field_help, group_intro
from src.settings.rows import list_rows
from src.settings.service import canonical_doc
from src.web.settings.backup import register_backup_routes
from src.web.settings.companies import register_companies_routes
from src.web.settings.content_draft import register_content_draft_routes
from src.web.settings.forms import apply_patch, decode, decode_secrets, errors_by_path
from src.web.settings.history import register_history_routes
from src.web.settings.probes import ProbeResult, probe_discord, probe_llm, probe_ntfy
from src.web.settings.readiness import check
from src.web.settings.rows import register_row_routes
from src.web.settings.sections import (
    Section, advanced_group, advanced_groups, group_section, section_by_slug, section_fields,
)
from src.web.settings.shell import page_ctx as _base_page_ctx, render_section

# profile has its own page; the rest share the generic editor.
EDITABLE_KINDS: tuple[str, ...] = tuple(k for k in DOCUMENT_KINDS if k != "profile")


# The {probe} URL slug -> (secret it tests, probe to run). Each value is a
# lambda, not the bare function, so every call looks `probe_ntfy` /
# `probe_discord` up on this module fresh — that's what lets tests
# monkeypatch `src.web.settings.routes.probe_ntfy` and have it take effect
# here. This is the single source of truth for the probe mapping: page_ctx's
# probe_targets (secret -> slug, for the per-secret Test button) is derived
# from it below rather than hand-kept as its own inverse, so the two can
# never drift apart.
_SINK_PROBES = {
    "ntfy": ("ntfy_topic_url", lambda url: probe_ntfy(url)),
    "discord": ("discord_webhook_url", lambda url: probe_discord(url)),
    "ops_ntfy": ("ops_ntfy_topic_url", lambda url: probe_ntfy(url)),
    "ops_discord": ("ops_discord_webhook_url", lambda url: probe_discord(url)),
}


def _probe_partial(request: Request, result: ProbeResult) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request, "_probe_result.html", {"result": result}
    )


def _probe_targets() -> dict[str, str]:
    """Secret name -> the {probe} slug it tests, derived fresh from
    _SINK_PROBES on every call (never cached) so a test that monkeypatches
    _SINK_PROBES after the app is built still sees the button appear."""
    return {secret: slug for slug, (secret, _fn) in _SINK_PROBES.items()}


def page_ctx(request: Request, section, **extra) -> dict:
    """shell.page_ctx with this module's own probe-target mapping filled in —
    every call in this module goes through here (or _render below) rather
    than shell.page_ctx directly, so no call site can forget it and silently
    lose its section's Test buttons."""
    return _base_page_ctx(request, section, probe_targets=_probe_targets(), **extra)


def _render(request: Request, section, **extra) -> HTMLResponse:
    return render_section(request, section, probe_targets=_probe_targets(), **extra)


def shown(ctx_submitted, cfg, spec):
    """What an input should display: what the user typed if this render follows
    a failed save, otherwise the stored value."""
    # A read-only or rows field is never submitted by a section form, so
    # echoing the form would render it as empty after a failed save. Always
    # read those from the config — a rows field as addressable Row objects
    # (digest + item-relative values), so the field macro's `rows` branch can
    # link each entry's Edit/Remove routes without recomputing digests itself.
    if ctx_submitted is None or spec.kind in NON_FORM_KINDS:
        if spec.kind == KIND_ROWS:
            return list_rows(cfg, spec.path)
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
    service = request.app.state.service
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

    # decode() emits one entry per field the page actually submits (every
    # checkbox, submitted or not — that's what makes an unchecked one work —
    # plus every other field that was on the page at all) — so a resubmission
    # of the whole rendered form still comes back with a full set of entries.
    # Keep only the ones that actually move a path away from what's in effect
    # now: apply_patch's own "changed" test
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
        service.update_settings(mutate, source="ui")
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

    # Secrets are not part of the settings document, so they get their own
    # writes — and only after the settings patch above has already succeeded,
    # so a validation failure leaves neither changed.
    if section.secrets:
        to_set, to_clear = decode_secrets(section.secrets, raw, service.secret_source)
        for name, value in to_set.items():
            service.set_secret(name, value)
        for name in to_clear:
            service.clear_secret(name)

    # Re-read so the page shows what was actually stored, not what was typed.
    request.state.snapshot = service.snapshot()
    return _render(request, section, saved=True, **extra)


def register_settings_routes(app: FastAPI) -> None:
    register_row_routes(app)
    register_companies_routes(app)
    register_history_routes(app)
    register_backup_routes(app)
    register_content_draft_routes(app)

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

    @app.get("/settings/profile", response_class=HTMLResponse)
    def profile_page(request: Request):
        return _render(request, section_by_slug("profile"),
                       body=request.state.snapshot.documents.profile or "")

    @app.post("/settings/profile", response_class=HTMLResponse)
    async def profile_save(request: Request):
        form = await request.form()
        body = str(form.get("body", ""))
        section = section_by_slug("profile")
        try:
            request.app.state.service.save_document("profile", body, source="ui")
        except SettingsInvalid as exc:
            _, form_level = errors_by_path(exc, known=set())
            return request.app.state.templates.TemplateResponse(
                request, section.template,
                page_ctx(request, section, form_errors=form_level, body=body),
            )
        request.state.snapshot = request.app.state.service.snapshot()
        return _render(request, section, saved=True,
                       body=request.state.snapshot.documents.profile or "")

    @app.get("/settings/documents", response_class=HTMLResponse)
    def documents_page(request: Request, kind: str = EDITABLE_KINDS[0]):
        if kind not in EDITABLE_KINDS:
            raise HTTPException(status_code=400, detail="unknown document kind")
        return _render(request, section_by_slug("documents"), kind=kind,
                       kinds=EDITABLE_KINDS,
                       body=getattr(request.state.snapshot.documents, kind) or "")

    @app.post("/settings/documents", response_class=HTMLResponse)
    async def documents_save(request: Request):
        form = await request.form()
        kind, body = str(form.get("kind", "")), str(form.get("body", ""))
        if kind not in EDITABLE_KINDS:
            raise HTTPException(status_code=400, detail="unknown document kind")
        section = section_by_slug("documents")
        try:
            request.app.state.service.save_document(kind, body, source="ui")
        except SettingsInvalid as exc:
            _, form_level = errors_by_path(exc, known=set())
            return request.app.state.templates.TemplateResponse(
                request, section.template,
                page_ctx(request, section, form_errors=form_level,
                         kind=kind, kinds=EDITABLE_KINDS, body=body),
            )
        request.state.snapshot = request.app.state.service.snapshot()
        return _render(request, section, saved=True, kind=kind, kinds=EDITABLE_KINDS,
                       body=getattr(request.state.snapshot.documents, kind) or "")

    @app.post("/settings/llm/test/llm", response_class=HTMLResponse)
    async def llm_test(request: Request):
        """Probe the values currently in the form — nothing is saved."""
        form = await request.form()
        raw = {k: form.getlist(k) for k in form.keys()}
        section = section_by_slug("llm")
        specs = section_fields(section)
        service = request.app.state.service
        snap = request.state.snapshot
        try:
            patch = decode(specs, raw)
        except SettingsInvalid as exc:
            return _probe_partial(request, ProbeResult(False, str(exc)))
        doc = dict(canonical_doc(snap.cfg))
        apply_patch(doc, patch)
        try:
            cfg = service.parse_settings(doc)
        except SettingsInvalid as exc:
            return _probe_partial(request, ProbeResult(False, str(exc)))
        # parse_settings() drops secrets (they are never part of the settings
        # document), so the probe re-attaches the effective ones: what's
        # already in effect (env, else stored) for every secret this section
        # owns, overlaid with whatever the form has typed but not yet saved.
        to_set, to_clear = decode_secrets(section.secrets, raw, service.secret_source)
        effective = {name: service.effective_secret(name) for name in section.secrets}
        effective.update(to_set)
        # A pending "clear" checkbox is what Save will actually do to this
        # secret — the probe must reflect that too, or ticking clear and
        # pressing Test reports green off the still-stored value.
        for name in to_clear:
            effective[name] = ""
        cfg = cfg.model_copy(update={"secrets": Secrets(**effective)})
        result = await probe_llm(cfg, snap.documents.profile)
        return _probe_partial(request, result)

    @app.post("/settings/notifications/test/{probe}", response_class=HTMLResponse)
    async def notifications_test(request: Request, probe: str):
        """Probe the URL in the form; fall back to the effective secret when the
        field was left blank (it renders blank when a value is already stored)."""
        entry = _SINK_PROBES.get(probe)
        if entry is None:
            raise HTTPException(status_code=404)
        name, run = entry
        form = await request.form()
        url = str(form.get(f"secret.{name}", "")).strip()
        if not url:
            url = request.app.state.service.effective_secret(name)
        return _probe_partial(request, await run(url))

    @app.get("/settings/advanced", response_class=HTMLResponse)
    def advanced_index(request: Request):
        return _render(request, section_by_slug("advanced"), groups=advanced_groups())

    @app.get("/settings/advanced/{key}", response_class=HTMLResponse)
    def advanced_page(request: Request, key: str):
        group = advanced_group(key)
        if group is None:
            raise HTTPException(status_code=404)
        # register_row_routes' add/update/remove redirect here with
        # ?added=1 / ?changed=1 / ?removed=1 after a row write; nothing used
        # to read it, so the "Saved" banner never fired for one.
        saved = any(request.query_params.get(f) == "1" for f in ("added", "changed", "removed"))
        return _render(request, group_section(group), group=group, saved=saved)

    @app.post("/settings/advanced/{key}", response_class=HTMLResponse)
    async def advanced_save(request: Request, key: str):
        group = advanced_group(key)
        if group is None:
            raise HTTPException(status_code=404)
        return await save_section(request, group_section(group), group=group)

    @app.get("/settings/{slug}", response_class=HTMLResponse)
    def section_page(request: Request, slug: str):
        section = section_by_slug(slug)
        if section is None:
            raise HTTPException(status_code=404)
        return _render(request, section)

    @app.post("/settings/{slug}", response_class=HTMLResponse)
    async def section_save(request: Request, slug: str):
        section = section_by_slug(slug)
        # A section with neither claimed paths nor claimed secrets has
        # nothing save_section could write (profile/documents *do* have
        # dedicated literal routes above, matched first, for their own
        # non-patch save shape; overview/advanced have no editable form at
        # all yet) — everything else, including a secrets-only section like
        # integrations, saves through the same generic path.
        if section is None or not (section.paths or section.secrets):
            raise HTTPException(status_code=404)
        # A section with bulk_save=False has no generic form save; every write
        # goes through its own row-specific routes.
        if not section.bulk_save:
            raise HTTPException(status_code=404)
        return await save_section(request, section)
