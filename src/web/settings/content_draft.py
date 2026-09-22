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
import hashlib
import json
import logging
import re
import time
import uuid
from typing import Any, Mapping

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from src.resume_intake.content_draft import MIN_TIMEOUT_SECONDS, NO_RESUME, draft_content
from src.resume_intake.errors import DraftFailed
from src.resume_intake.facts_scaffold import build_facts_yaml
from src.settings.errors import SettingsInvalid
from src.web.settings.sections import section_by_slug
from src.web.settings.shell import page_ctx

log = logging.getLogger(__name__)

KEY = "content_draft"
PAGE_TEMPLATE = "settings_content_draft.html"
RESULT_TEMPLATE = "_content_draft_result.html"
PATH = "/settings/documents/draft"

# The stamp a draft carries when there was no resume_content document at all
# when it was made. A sentinel rather than the digest of an empty string:
# "there was nothing here" and "there was an empty document here" are
# different situations, and only the first is a first save.
NO_DOCUMENT = "absent"

STALE_RUNNING_ERROR = (
    "That draft never finished — the server was most likely restarted while "
    "it was running. Nothing was written to your résumé data. Try again, or "
    "start over."
)


def _content_digest(snap) -> str:
    """A stamp for the resume_content document as it stands in ``snap``.

    A digest of the document text rather than the settings version id: the
    version bumps on every settings save (a filter tweak, a new company), and
    refusing a résumé save because something unrelated moved would teach the
    user to tick the confirm box without reading it. Hashing one document is
    free next to the minute-plus LLM call that produced the draft."""
    text = snap.documents.resume_content
    if text is None:
        return NO_DOCUMENT
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _stale_after_seconds(cfg) -> float:
    """How long a "running" record may sit before the page stops believing it.

    Anchored on the drafting call's own effective timeout — the same
    max(configured, floor) expression draft_content builds its binding with —
    and then doubled for headroom, because the provider answering is not the
    end of a run: assign_ids, three emptiness guards and a parse_content
    round-trip all follow it, then a store write. A bound below the call's own
    timeout would declare a live draft dead and invite the user to start a
    second one racing the first; a bound far above it leaves a bricked page
    bricked for longer than it has to be. With the 180s floor that puts the
    default at six minutes, comfortably past the one-to-three-minute call this
    feature actually makes."""
    return 2 * max(cfg.resume_draft.timeout_seconds, MIN_TIMEOUT_SECONDS)


def claim_run(store) -> str:
    """Make a new run the owner of the record, and return its id.

    One draft at a time: whoever holds the record's run_id is the only run
    allowed to write it. Discarding deletes the record and a new start
    replaces it, and either way a run still in flight finds it no longer owns
    the key and drops its result instead of resurrecting a draft the user
    threw away, or overwriting the one they are now reading."""
    run_id = uuid.uuid4().hex
    store.put(KEY, {"status": "running", "started_at": time.time(), "run_id": run_id})
    return run_id


def _owns(store, run_id: str) -> bool:
    record = store.get(KEY)
    return bool(record) and record.get("run_id") == run_id


def _cancel_running(app) -> None:
    """Stop the in-flight run, if any, so it spends no more LLM time on a
    draft nobody will read. Ownership is what keeps its result off the
    record; this only saves the call. Must run on the event loop — a task is
    not safe to cancel from another thread."""
    task = getattr(app.state, "content_draft_task", None)
    if task is not None and not task.done():
        task.cancel()


async def run_content_draft(app, run_id: str) -> dict:
    """Run the draft and store its record, if ``run_id`` still owns it. Never
    raises: a drafting failure is a message on a page. Writes no document —
    only the review form's save does, which is what makes the review a real
    gate.

    The ownership checks and the writes they guard have no ``await`` between
    them, and every route that deletes or replaces the record runs on the
    event loop too, so nothing can slip in between a check and its write."""
    store = app.state.stores.wizard
    # Two stamps, carried onto whichever record this run ends with.
    #
    # started_at is what lets the page tell a live run from one whose process
    # died mid-call: without it a "running" record left behind by a restart
    # polls forever, and both entry points lead only there.
    #
    # content_digest is what lets the save route tell whether the document
    # this draft was proposed against is still the one the save would replace.
    # It deliberately describes the document as it stood when the call
    # STARTED, not when it finished: a document hand-written during the
    # minute-plus call is just as much lost work as one written after it.
    stamp: dict = {"started_at": time.time(), "run_id": run_id}
    try:
        snap = app.state.service.snapshot()
        stamp["content_digest"] = _content_digest(snap)
        if _owns(store, run_id):
            store.put(KEY, {"status": "running", **stamp})
        draft = await draft_content(snap.cfg, resume_text=snap.documents.resume_text or "")
        record = {"status": "ok", "document": draft.document, "contact": draft.contact}
    except DraftFailed as exc:
        record = {"status": "error", "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 — a failed draft is a message, not a 500
        log.warning("content_draft_task_failed",
                    extra={"error": str(exc), "error_type": type(exc).__name__})
        record = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
    # Returned as stored, stamp included — a caller that inspects the result
    # and a caller that reads the store back must not see two shapes.
    record = {**record, **stamp}
    if not _owns(store, run_id):
        log.info("content_draft_run_superseded", extra={"status": record["status"]})
        return record
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
    experience-only result is fine as long as something remains. An entry
    blanked down to no company AND no position (or, for a project, no name)
    goes the same way, for the same reason and to match assign_ids.
    """

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
    # Two reasons an entry does not survive the review. No bullets left:
    # documented above. Neither a company nor a position left: the same rule
    # assign_ids applies to the model's own answer
    # (src/resume_intake/content_draft.py, experiences loop), applied here to
    # the user's edits for consistency — blanking both fields means "delete
    # this role", not "write a nameless one". parse_content accepts an entry
    # whose company and role are both "", and the renderer would then print
    # an empty heading over real bullets.
    experiences = [e for e in experiences if e["bullets"] and (e["company"] or e["role"])]
    projects = [
        {**p,
         "name": _one(form, f"name.{p['id']}", p.get("name", "")),
         "subtitle": _one(form, f"subtitle.{p['id']}", p.get("subtitle", "")),
         "dates": _one(form, f"dates.{p['id']}", p.get("dates", "")),
         "bullets": edited_bullets(p.get("bullets"))}
        for p in document.get("projects") or []
    ]
    # Same, for the one field a project is named by: assign_ids requires a
    # name, and a project has no second heading to fall back on.
    projects = [p for p in projects if p["bullets"] and p["name"]]

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


def _current(request: Request) -> dict | None:
    """The stored record as the page should present it.

    A "running" record older than _stale_after_seconds cannot finish: the task
    that would have replaced it died with its process — a docker restart, a
    ``--build``, an autoreload mid-call. Left as "running" it renders a poll
    that never resolves, and both entry points (the /settings/documents banner
    and /wizard/done's "Draft it") lead only there, so the feature is bricked
    until someone edits the wizard_ui table by hand. Presented as an error,
    the page's own "Try again" and "Start over" forms recover it.

    Presented, not rewritten: a GET must not write, and a task that somehow is
    still alive still owns the key and will overwrite it with its real result.
    A record carrying no started_at at all was written before this check
    existed, which is precisely the stuck state it recovers — so it counts as
    stale rather than as ageless."""
    record = request.app.state.stores.wizard.get(KEY)
    if not record or record.get("status") != "running":
        return record
    age = time.time() - float(record.get("started_at") or 0)
    if age <= _stale_after_seconds(request.state.snapshot.cfg):
        return record
    return {"status": "error", "error": STALE_RUNNING_ERROR}


def _render(request: Request, *, draft: dict | None = None, status_code: int = 200,
            **extra) -> HTMLResponse:
    """The Documents section's nav and chrome, this module's template.

    ``draft`` overrides the stored record when given — the save route passes
    the record with its ``document`` swapped for apply_edits's output when
    re-rendering after a refusal, so the user's typed text and drop choices
    survive the redisplay instead of reverting to the last stored draft."""
    return request.app.state.templates.TemplateResponse(
        request, PAGE_TEMPLATE,
        page_ctx(request, section_by_slug("documents"),
                 draft=_current(request) if draft is None else draft,
                 **extra),
        status_code=status_code,
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

        # One draft at a time. A live run keeps the record: a second tab or a
        # double-submit lands back on its poll instead of racing it. A run
        # past the staleness bound reads as an error, so its "Try again"
        # still gets through — and replaces that run below if it was merely
        # slow rather than dead.
        current = _current(request)
        if current and current.get("status") == "running":
            return RedirectResponse(PATH, status_code=303)

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
        # event loop. Same ordering as wizard_preview_start. started_at goes
        # on this write too, not only on the task's own: a process that dies
        # between the two would otherwise leave an unstamped record that
        # _current could not age out by timestamp.
        _cancel_running(request.app)
        run_id = claim_run(store)
        # Keep a reference so the task is not garbage-collected mid-run.
        request.app.state.content_draft_task = asyncio.create_task(
            run_content_draft(request.app, run_id))
        return RedirectResponse(PATH, status_code=303)

    @app.get(f"{PATH}/status", response_class=HTMLResponse)
    def content_draft_status(request: Request):
        # confirm_replace is passed explicitly rather than left to Jinja's
        # default undefined: this route renders the result partial with its
        # own bare context, and a name that is only ever silently falsy is
        # the kind of thing a later reader deletes from the template.
        return request.app.state.templates.TemplateResponse(
            request, RESULT_TEMPLATE,
            {"draft": _current(request), "confirm_replace": False},
        )

    @app.post(f"{PATH}/discard")
    async def content_draft_discard(request: Request):
        """Throw the pending draft away.

        The way out of what used to be two one-way doors: a "running" record
        nothing else clears, and an "ok" draft the user simply does not like,
        whose only other exits were saving it or hand-editing JSON. Writes no
        document — discarding a proposal is not a change to the résumé — and
        lands back on the page, which then offers a fresh draft (through the
        confirm gate, if a document exists).

        A run still in flight is cancelled, and having lost the record it
        could not write its result anyway (see claim_run). ``async`` on
        purpose: a plain ``def`` route runs in a worker thread, where
        cancelling the task is unsafe and the delete could land between the
        task's ownership check and its write."""
        _cancel_running(request.app)
        request.app.state.stores.wizard.delete(KEY)
        return RedirectResponse(PATH, status_code=303)

    @app.post(f"{PATH}/save", response_class=HTMLResponse)
    async def content_draft_save(request: Request):
        store = request.app.state.stores.wizard
        record = _current(request) or {}
        if record.get("status") == "running":
            # A raw {"detail": ...} 409 loses the page along with the edits.
            # What has actually happened is that a newer draft replaced the
            # one being reviewed, and the page the user is standing on is the
            # only honest place to say so. Their typed text cannot be carried
            # over — the draft it was edits TO is gone — so the message says
            # plainly that this review is no longer the current one rather
            # than implying the text was kept. The 409 stays: it is still a
            # conflict, it is just one with an explanation attached.
            return _render(request, status_code=409, form_errors=[
                "A newer draft is still running, so the review you just "
                "submitted is no longer the current one. Nothing was saved — "
                "wait for the new draft to finish and review that instead."])
        if record.get("status") != "ok":
            # Same reasoning as above: a raw {"detail": ...} 409 loses the
            # page. Discarded elsewhere, never finished (a stale "running"
            # record, which _current presents as an error), or failed — the
            # page underneath shows whichever it was, with its own way on.
            return _render(request, status_code=409, form_errors=[
                "Nothing was saved: there is no finished draft to save any "
                "more. It was discarded, or it never finished."])

        form = await request.form()
        raw = {k: form.getlist(k) for k in form.keys()}
        document = apply_edits(record["document"], raw)
        service = request.app.state.service
        # Used below for the SettingsInvalid re-render: the user's typed
        # text is valid input the validator rejected for some other reason,
        # and it must survive the redisplay rather than silently reverting.
        edited_record = {**record, "document": document}

        # The start POST's confirm gate covers resume_content as it stood when
        # the draft was made. It cannot cover a document written SINCE — and
        # this page's own "Edit as JSON instead" link is the shortest route
        # into exactly that: draft, go hand-write resume_content, come back to
        # a review form still sitting on screen, save, and the hand-written
        # document is replaced with no warning. So the draft's stamp is
        # re-checked here and the save refused by default, the same
        # refuse-then-opt-in shape the start gate and the settings import
        # guard use (src/settings/transfer.py:66-72).
        #
        # Deliberately not "the digest moved": what matters is that a document
        # would be replaced whose content this draft never saw. With nothing
        # there now, the save replaces nothing and there is nothing to confirm
        # — which is also what keeps a record stamped before this check
        # existed (no content_digest at all) from demanding a confirmation
        # that would name a document the user does not have.
        in_effect = _content_digest(request.state.snapshot)
        would_replace = (in_effect != NO_DOCUMENT
                         and record.get("content_digest") != in_effect)

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
            return _render(request, draft=record, confirm_replace=would_replace,
                           form_errors=[
                "Every bullet was dropped or blank, so there would be nothing "
                "to tailor. Keep at least one, or edit the document as JSON."])

        # Checked after the nothing-left refusal on purpose: that refusal
        # writes nothing either way, so warning about a replacement that was
        # never going to happen would be two problems reported as one.
        if would_replace and not form.get("overwrite"):
            return _render(request, draft=edited_record, confirm_replace=True)

        try:
            service.save_document(
                "resume_content",
                json.dumps(document, indent=2, ensure_ascii=False),
                source="llm_draft",
            )
        except SettingsInvalid as exc:
            # confirm_replace rides along: the checkbox has to come back with
            # the form, or a user who already ticked it would be refused a
            # second time on their next submit for a replacement they have
            # already agreed to.
            return _render(request, draft=edited_record, confirm_replace=would_replace,
                           form_errors=[str(exc)])

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
