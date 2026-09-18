"""The settings pages.

Route order matters: /settings/advanced is declared before /settings/{slug},
because Starlette matches in registration order and the parameterised route
would otherwise swallow it."""
from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from src.config import Secrets
from src.settings.documents import DOCUMENT_KINDS
from src.settings.errors import NotConfigured, SettingsInvalid, StaleWrite
from src.settings.fields import (
    KIND_BOOL, KIND_CHIPS, KIND_MULTI_CHOICE, KIND_READ_ONLY, optional_groups, value_at,
)
from src.settings.help import field_help, group_intro
from src.settings.service import canonical_doc, secret_env_var
from src.web.settings.forms import apply_patch, decode, decode_secrets, errors_by_path
from src.web.settings.probes import ProbeResult, probe_discord, probe_llm, probe_ntfy
from src.web.settings.readiness import check
from src.web.settings.sections import (
    Section, SECTIONS, advanced_group, advanced_groups, group_section, section_by_slug,
    section_fields,
)

# profile has its own page; the rest share the generic editor.
EDITABLE_KINDS: tuple[str, ...] = tuple(k for k in DOCUMENT_KINDS if k != "profile")


# Secrets masked as type="password" — a name ending in one of these never
# renders its stored value either way, so masking only affects what the user
# can see while typing or pasting a new one. That's worth it for an API key,
# but not for a pasted URL or identifier (ntfy topic, Discord webhook,
# tailor_endpoint_url, ...): those can't be proofread before submit if
# masked, and several of their pages (integrations) have no Test button, so a
# typo fails silently until the feature breaks.
_MASKED_SECRET_SUFFIXES = ("_key", "_password", "_secret")

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


def secret_rows(service, names) -> list[dict]:
    """{name, source, env_var, label, masked} for each of a section's secrets
    — the template never sees the value, only where it currently comes from."""
    return [
        {
            "name": name,
            "source": service.secret_source(name),
            "env_var": secret_env_var(name),
            "label": name.replace("_", " "),
            "masked": name.endswith(_MASKED_SECRET_SUFFIXES),
        }
        for name in names
    ]


def _probe_partial(request: Request, result: ProbeResult) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request, "_probe_result.html", {"result": result}
    )


def page_ctx(request: Request, section, **extra) -> dict:
    """The context every section template expects. Values render from the
    validated AppConfig on the request snapshot, never from the stored doc —
    the doc is sparse (canonical_doc drops defaults), the config is complete."""
    ctx = {
        "sections": SECTIONS,
        "section": section,
        "cfg": request.state.snapshot.cfg,
        "fields": section_fields(section) if section.paths else (),
        "secrets": secret_rows(request.app.state.service, section.secrets),
        "errors": {},
        "form_errors": [],
        "saved": False,
        "submitted": None,
        # Secret name -> the {probe} slug it tests, for secret_field's
        # per-secret Test button — derived from _SINK_PROBES above so this
        # can't drift out of sync with it. A secret with no entry here (most
        # of them, including every secret integrations and llm own) simply
        # renders no button.
        "probe_targets": {secret: slug for slug, (secret, _fn) in _SINK_PROBES.items()},
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
        return _render(request, group_section(group), group=group)

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
        return await save_section(request, section)
