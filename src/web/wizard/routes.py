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
from src.resume_intake.draft import draft_profile_and_filters
from src.resume_intake.errors import DraftFailed
from src.resume_intake.extract import ExtractionFailed, extract_text
from src.resume_intake.interview import INTERVIEW_FIELDS, answers_to_patch, decode_answers
from src.settings.boards import board_entries
from src.settings.documents import validate_document
from src.settings.errors import NotConfigured, SettingsInvalid, StaleWrite
from src.settings.fields import field_map, value_at
from src.settings.patch import apply_patch
from src.settings.service import canonical_doc
from src.starter_pack import default_pack
from src.web.settings.backup import MAX_UPLOAD_BYTES, _read_bounded
from src.web.settings.forms import decode, decode_secrets, errors_by_path
from src.web.settings.probes import ProbeResult, probe_discord, probe_llm, probe_ntfy
from src.web.settings.readiness import AGGREGATOR_FAMILIES, check
from src.web.settings.routes import _probe_partial, shown
from src.web.settings.sections import section_by_slug
from src.web.settings.shell import secret_rows
from src.web.wizard.ntfy_topic import suggest_topic, topic_qr_svg
from src.web.wizard.presets import LLM_PRESETS, preset_for_provider
from src.web.wizard.title_sets import TITLE_SETS
from src.web.wizard.steps import (
    WIZARD_STEPS, build_context, next_step, step_by_slug, step_states,
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
# Identical to settings' own "llm" section secrets — derived from it (like
# FILTER_PATHS below) rather than hand-kept as a second copy that could drift.
LLM_SECRETS = section_by_slug("llm").secrets

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
    viewed = step.slug if step else None
    states = step_states(current_context(request), request.app.state.stores.wizard.skipped(),
                          viewed=viewed)
    slugs = [s.slug for s in WIZARD_STEPS]
    position = slugs.index(viewed) if viewed else None
    ctx = {
        "step": step,
        "steps": states,
        # Rail header and the action bar's Back link. None on /wizard/done.
        "step_number": position + 1 if position is not None else None,
        "step_count": len(WIZARD_STEPS),
        "prev_href": f"/wizard/{slugs[position - 1]}" if position else None,
        # Whether THIS render's own step is done, for a step page's own
        # Continue/Finish control: linking straight to /wizard only advances
        # when the step is actually complete (next_step() would otherwise
        # just route right back to it) — see wizard_companies.html and
        # wizard_preview.html.
        "step_done": bool(step) and any(s.step.slug == step.slug and s.done for s in states),
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


# wizard_ui store key marking that the companies step has been submitted at
# least once — set by wizard_companies_save below. Its PRESENCE, not its
# value, is what companies_extra reads to decide whether to pre-tick the two
# checkboxes: a config with neither flag set means either "never asked"
# (pre-tick) or "asked and declined" (don't re-tick), and only this marker
# tells the two apart.
COMPANIES_CHOICE_KEY = "companies_choice"

AGGREGATOR_LABELS = {
    "hn_who_is_hiring": "Hacker News Who's Hiring",
    "remotive": "Remotive",
    "remoteok": "RemoteOK",
    "hiringcafe": "hiring.cafe",
    "adzuna": "Adzuna",
}


def companies_extra(request: Request) -> dict:
    """Slug-family boards already configured, for the companies step's
    "Already configured" nudge. Only SLUG_SOURCE_FAMILIES (src/config.py) --
    each entry there is a bare slug string. Structured families (Workday,
    Oracle Cloud, ...) still count toward the step's own completion check
    (steps.py's _companies_done reads STRUCTURED_FAMILIES too) but aren't
    enumerated here: their entries are structured boards, not slugs, so
    "family: slug" isn't a sensible display for them, and the "Full companies
    page" link is where they're actually managed.

    Also the starter-pack/discovery checkbox state: pre-ticked until the step
    has actually been submitted once (COMPANIES_CHOICE_KEY absent), after
    which they reflect the saved config — see that key's own docstring
    above."""
    cfg = request.state.snapshot.cfg
    chosen = request.app.state.stores.wizard.get(COMPANIES_CHOICE_KEY) is not None
    extra = {
        "configured": [
            (family, slug)
            for family in SLUG_SOURCE_FAMILIES
            for slug in getattr(cfg.sources, family, ()) or ()
        ],
        # Read from config rather than hardcoded: an install that switched the
        # aggregators off must not be told they are running.
        "always_on": [
            AGGREGATOR_LABELS[family]
            for family in AGGREGATOR_FAMILIES
            if getattr(getattr(cfg.sources, family, None), "enabled", False)
        ],
        "pack_count": default_pack().count(cfg.discovery.eu_seeds_enabled),
        "pack_checked": cfg.discovery.starter_pack if chosen else True,
        "discovery_checked": cfg.discovery.enabled if chosen else True,
    }
    return extra


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
    suggested_topic/suggested_qr ONLY when ntfy_topic_url is currently unset.

    That gate matters: suggest_topic() is random, and re-suggesting on every
    render would silently swap out a topic the user may already have scanned
    into the ntfy app on their phone. Once the topic is saved,
    service.secret_source(...) reads "stored" (or "env"), the gate closes,
    and the template falls back to secret_field's ordinary stored-source
    notice instead of a suggestion.

    The suggestion itself is generated at most ONCE per install (not once per
    GET — a fresh one per GET is exactly the bug this guards against: a user
    can scan the QR, the page can re-render for any unrelated reason, and a
    second suggest_topic() call would hand them a topic their phone never
    subscribed to, with no error to say so — on ntfy.sh the topic itself IS
    the credential, so that failure is silent and total). It is cached in the
    wizard_ui store (keyed "suggested_ntfy_topic") and reused across every GET
    until the secret is actually set, at which point wizard_notifications_save
    below clears the cache — there's nothing left to suggest once a topic is
    stored, and a later revisit after clearing that secret should get a fresh
    one rather than resurrecting whatever was suggested and never used the
    first time around. Its result is reused for both the pre-filled input and
    the QR (topic_qr_svg(topic)) — never called twice per render either, so
    the two can never disagree."""
    extra: dict = {"probe_targets": _wizard_probe_targets()}
    if request.app.state.service.secret_source("ntfy_topic_url") == "unset":
        store = request.app.state.stores.wizard
        topic = store.get("suggested_ntfy_topic")
        if topic is None:
            topic = suggest_topic()
            store.put("suggested_ntfy_topic", topic)
        extra["suggested_topic"] = topic
        extra["suggested_qr"] = topic_qr_svg(topic)
    return extra


def review_prefill(request: Request) -> dict:
    """What the filter inputs start out showing.

    Once a profile document has actually been saved, this page is an ordinary
    settings page from then on: the config is truth, so no prefill (an empty
    mapping makes prefilled_shown() fall straight through to the config). That
    matters for a direct revisit after approval — without it, a stale
    pre-edit draft cached from before that save would keep outranking the
    config forever, so an intentional edit made at approval time would look
    like it silently reverted on the next visit.

    Before that first save, the interview answers are the floor — they already
    bind to real config paths — and the draft is layered over them, path by
    path. Where the draft set a path it wins, being built FROM those answers
    and so the later and better-informed word; every path it left alone keeps
    what the user themselves said.

    Layering rather than replacing matters because a draft is "ok" as soon as
    its profile parses, while _sanitize() independently drops any filter path
    the model got wrong — a model answering with bare `titles` instead of
    `filters.titles` loses all of them and still reports "ok". Replacing the
    answers with that draft would hand back exactly the empty form this is
    here to prevent, and a partial draft would blank the rest."""
    if request.state.snapshot.documents.profile:
        return {}
    store = request.app.state.stores.wizard
    draft = store.get("draft") or {}
    prefill = answers_to_patch(store.get("answers") or {})
    if draft.get("status") == "ok":
        prefill.update(draft.get("filters") or {})
    return prefill


def prefilled_shown(ctx_submitted, cfg, spec, prefill):
    """shown() with one more layer underneath, for the steps that open with a
    suggestion: the same submitted-or-config rule settings pages use
    (settings.routes.shown), except that a step's prefill wins over the stored
    config, so a first visit shows the suggestion rather than empty/default
    fields. Once the user has
    actually typed something (ctx_submitted is not None, i.e. a re-render
    after a failed save), that submitted value always wins, exactly like
    shown() — this never overrides what's on screen with the draft."""
    if ctx_submitted is None and spec.path in prefill:
        return prefill[spec.path]
    return shown(ctx_submitted, cfg, spec)


def llm_prefill(request: Request) -> dict:
    """The LLM step opens with Enabled already ticked.

    The shipped default stays false on purpose: _llm_done() uses
    relevance.enabled as its "has the user decided anything here?" signal, so a
    true default would mark this step complete on a fresh install and skip the
    one screen where the provider, model and key get set. Pre-ticking the box
    puts the recommended path in front of the user without touching either the
    config default or the step logic.

    Skipping is the only way past this step without enabling scoring, so a
    skipped step is the user saying no — and re-ticking then would quietly walk
    that back."""
    if "llm" in request.app.state.stores.wizard.skipped():
        return {}
    return {"relevance.enabled": True}


def llm_extra(request: Request) -> dict:
    cfg = request.state.snapshot.cfg
    return {
        "prefill": llm_prefill(request),
        "presets": LLM_PRESETS,
        # Open the panel for the provider already configured, so the
        # instructions describe the form the user is actually looking at.
        "open_recipe": preset_for_provider(cfg.relevance.provider, cfg.relevance.ollama_host),
    }


def review_extra(request: Request) -> dict:
    """The review step's own context: the prefill mapping described above, and
    the profile document itself when one is already saved — so the profile
    textarea (which has no config path to read a "current value" from the way
    the filter fields do) also shows what was actually saved rather than a
    stale draft, on a revisit after approval."""
    return {
        "prefill": review_prefill(request),
        "saved_profile": request.state.snapshot.documents.profile,
        "required": {
            WARNING_PATHS[code][0]: WARNING_PATHS[code][1]
            for code in STEP_WARNING_CODES["review"]
            if code in WARNING_PATHS
        },
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


# Steps whose own primary button posts straight at /wizard/<slug>/skip when
# they are incomplete (see wizard_companies.html / wizard_preview.html): for
# these the shared "Skip for now" form would be a second button pointing at
# the identical URL, so the base template renders none.
OWN_SKIP_STEPS = frozenset({"companies", "preview"})


def render_step(request: Request, step, **extra) -> HTMLResponse:
    paths, secrets = STEP_FIELDS.get(step.slug, ((), ()))
    extra = {"own_skip": step.slug in OWN_SKIP_STEPS,
             "title_sets": TITLE_SETS, **extra}
    if step.slug == "llm":
        extra = {**llm_extra(request), **extra}
    elif step.slug == "companies":
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


# A step's slug -> the readiness codes (readiness.check()'s own Warning.code)
# that explain why it can still be incomplete after a save. Hand-kept rather
# than derived from Warning.fix_slug: that field names the SETTINGS section a
# warning links to (e.g. "filters" for no_titles), which does not always
# match the WIZARD step slug -- "review" covers both the profile and the
# filters together. A step absent here (companies, resume, preview) has no
# single check() code that maps onto its own completion test, so it never
# gets a warning through this path.
# The field each blocking warning is about. Keyed by the same readiness code
# STEP_WARNING_CODES uses, so the "you must fill this" shown up-front and the
# "you did not fill this" shown after a failed save cannot drift apart — and a
# new blocking check has to name its field (tests/web/wizard/
# test_required_fields.py fails otherwise).
#
# The value is WHY, not a rule. max_age_days is not inherently mandatory: it
# is mandatory here because leaving it unset makes the first run alert on
# every posting already on every board, and that is worth knowing while you
# choose the number rather than after being sent back.
WARNING_PATHS: dict[str, tuple[str, str]] = {
    "no_titles": ("filters.titles",
                  "Nothing matches a title you didn't list, so leaving this "
                  "empty means no posting can ever match."),
    "no_max_age": ("filters.max_age_days",
                   "Leave this unset and the first run alerts on every "
                   "posting already on every board, not just new ones."),
}


STEP_WARNING_CODES: dict[str, tuple[str, ...]] = {
    "llm": ("llm_no_key",),
    "review": ("no_titles", "no_max_age"),
    "notifications": ("no_sink",),
}


def step_warnings(request: Request, step) -> list[str]:
    """The unmet readiness warning(s) that explain why `step` is still
    incomplete, reusing check()'s own message text rather than a second copy
    of it. Callers only ask for this right after a save (see wizard_root's
    `attempted` handling below) -- a step's first, unattempted arrival never
    calls this, so a warning never appears before the user has tried
    anything."""
    codes = STEP_WARNING_CODES.get(step.slug, ())
    if not codes:
        return []
    snap = request.state.snapshot
    warnings = check(
        snap.cfg, has_profile=bool(snap.documents.profile),
        secret_source=request.app.state.service.secret_source,
    )
    return [w.message for w in warnings if w.code in codes]


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

    # `attempted` names the step just saved; wizard_root below only keeps it
    # (as ?attempted=1 on the step page) when /wizard still lands back on
    # this same step — i.e. the save did not actually finish it — so that
    # page can show step_warnings() instead of silently re-serving itself.
    return RedirectResponse(f"/wizard?attempted={step.slug}", status_code=303)


def register_wizard_routes(app: FastAPI) -> None:
    app.state.templates.env.globals["prefilled_shown"] = prefilled_shown

    @app.get("/wizard")
    def wizard_root(request: Request):
        """Always the entry point: recomputes where the user actually is, so a
        bookmarked /wizard resumes rather than restarting.

        `attempted`, when present, names the step a save handler just tried
        to complete (see save_step / wizard_review_save). It is forwarded to
        the step page as ?attempted=1 ONLY when this still lands back on
        that same step — i.e. the save did not finish it — so the step's own
        page can explain why instead of silently re-serving itself. Landing
        anywhere else (a different step, or done) drops it: that step was
        never attempted, so it gets no warning on this, its first arrival."""
        step = next_step(current_context(request), request.app.state.stores.wizard.skipped())
        if step is None:
            return RedirectResponse("/wizard/done", status_code=303)
        suffix = "?attempted=1" if request.query_params.get("attempted") == step.slug else ""
        return RedirectResponse(f"/wizard/{step.slug}{suffix}", status_code=303)

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
                # _read_bounded (src/web/settings/backup.py) reads in chunks
                # and stops the moment the running total passes the limit,
                # rather than upload.read() with no size — which would
                # buffer the whole body, however large, before anything
                # gets a chance to check it. Same MAX_UPLOAD_BYTES backup
                # uploads are bounded to; a résumé is never remotely close
                # to it, so the shared limit costs nothing here.
                data, oversized = await _read_bounded(upload, MAX_UPLOAD_BYTES)
                if oversized:
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
        response = save_step(request, step_by_slug("notifications"), (), NOTIFY_SECRETS, raw)
        # Once a topic is actually stored, notifications_extra's gate closes
        # for good on its own (it only reads the cache while secret_source
        # says "unset") — this is just hygiene, so a suggestion nobody used
        # doesn't sit in wizard_ui forever, the same reasoning the review
        # step's draft delete uses above.
        if request.app.state.service.secret_source("ntfy_topic_url") != "unset":
            request.app.state.stores.wizard.delete("suggested_ntfy_topic")
        return response

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
        step = step_by_slug("review")
        draft, error = await _ensure_draft(request)
        attempted_warnings = (
            step_warnings(request, step)
            if request.query_params.get("attempted") == "1" else []
        )
        return render_step(
            request, step, draft=draft,
            form_errors=([error] if error else []) + attempted_warnings,
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
        return RedirectResponse(f"/wizard?attempted={step.slug}", status_code=303)

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

    @app.post("/wizard/companies")
    async def wizard_companies_save(request: Request):
        """The Where-to-look step's one control: ticking the boxes turns the
        starter pack and/or discovery on, and leaving both unticked with
        nothing else configured is itself a valid choice (an explicit skip),
        not an error — Continue always posts here rather than to a bare
        `/wizard` link or a separate `/wizard/companies/skip`, so the button
        does the right thing either way (see wizard_companies.html)."""
        form = await request.form()
        want = {
            "discovery.starter_pack": form.get("starter_pack") == "1",
            "discovery.enabled": form.get("discovery") == "1",
        }
        cfg = request.state.snapshot.cfg
        patch = {p: v for p, v in want.items() if v != value_at(cfg, p)}
        service = request.app.state.service
        if patch:
            try:
                service.update_settings(
                    lambda doc: "wizard: Where to look" if apply_patch(doc, patch) else None,
                    source="wizard",
                )
            except StaleWrite:
                step = step_by_slug("companies")
                return render_step(request, step, form_errors=[
                    "Someone else saved while you were on this step. Reload and reapply."
                ])
            except NotConfigured:
                return RedirectResponse("/setup", status_code=303)
        wizard = request.app.state.stores.wizard
        wizard.put(COMPANIES_CHOICE_KEY, True)
        if not any(want.values()) and not board_entries(service.snapshot().cfg):
            wizard.skip("companies")
        return RedirectResponse("/wizard", status_code=303)

    # Declared before /wizard/{slug} — Starlette matches in registration
    # order, and the parameterised route would otherwise swallow /wizard/done
    # (slug="done"), the same ordering constraint documented at the top of
    # src/web/settings/routes.py. /wizard/review and its POST siblings above
    # are declared here for the same reason: /wizard/{slug} would otherwise
    # swallow GET /wizard/review too. POST /wizard/companies just above and
    # the two preview routes before it don't actually need this: /wizard/{slug}
    # below is GET-only, so a same-shaped POST is matched as a distinct route
    # by method regardless of registration order — they're grouped here for
    # readability, not correctness.
    @app.get("/wizard/{slug}", response_class=HTMLResponse)
    def wizard_step(request: Request, slug: str):
        step = step_by_slug(slug)
        if step is None:
            raise HTTPException(status_code=404)
        warnings = (
            step_warnings(request, step)
            if request.query_params.get("attempted") == "1" else []
        )
        return render_step(request, step, form_errors=warnings)

    @app.post("/wizard/{slug}/skip")
    def wizard_skip(request: Request, slug: str):
        if step_by_slug(slug) is None:
            raise HTTPException(status_code=404)
        request.app.state.stores.wizard.skip(slug)
        return RedirectResponse("/wizard", status_code=303)
