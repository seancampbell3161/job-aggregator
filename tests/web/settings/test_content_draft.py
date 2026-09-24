"""The drafting page: a background task, a polled status, and a confirm gate
in front of an existing document."""
import hashlib
import json
import time

from src.web.app import create_app
from src.web.settings.content_draft import NO_DOCUMENT, apply_edits
from tests.auth_helpers import signed_in_client
from tests.settings_helpers import WEB_TEST_SETTINGS, make_service

CONTENT_JSON = """{"name": "S", "skills": [], "experiences": [
  {"id": "e1", "company": "Acme", "role": "Staff",
   "bullets": [{"id": "e1-b1", "text": "did a thing"}]}]}"""


def _app(tmp_path, monkeypatch, *, documents=None):
    monkeypatch.setenv("JOB_AGG_SQLITE_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("JOB_AGG_TAILORED_DIR", str(tmp_path / "tailored"))
    return create_app(service=make_service(WEB_TEST_SETTINGS, documents=documents or {}))


def test_the_page_offers_drafting_when_nothing_has_run(tmp_path, monkeypatch):
    r = signed_in_client(_app(tmp_path, monkeypatch)).get("/settings/documents/draft")
    assert r.status_code == 200
    assert "Draft from my résumé" in r.text
    assert "<code>resume_content</code>" not in r.text
    assert "résumé content (for tailoring)" in r.text


def test_starting_without_a_resume_records_an_error_and_starts_no_task(tmp_path, monkeypatch):
    from src.resume_intake.content_draft import NO_RESUME

    app = _app(tmp_path, monkeypatch)
    tripped = False

    async def tripwire(app_, run_id):
        nonlocal tripped
        tripped = True

    monkeypatch.setattr("src.web.settings.content_draft.run_content_draft", tripwire)
    r = signed_in_client(app).post("/settings/documents/draft", follow_redirects=True)
    assert NO_RESUME in r.text
    assert tripped is False


def test_starting_records_running_before_the_task_can_run(tmp_path, monkeypatch):
    """The record is written synchronously, before create_task, so the page's
    first poll cannot race the event loop and find nothing.

    Checking the store only after the whole request/response round trip has
    finished can't actually tell the two orderings apart: nothing awaits
    between the store write and asyncio.create_task in either arrangement,
    so both statements always run before this test's own .post() call
    returns, and the fake_run stand-in never touches the store itself
    either way — confirmed by mutation testing (swapping the two lines left
    this assertion green 20/20 runs). So this spies on asyncio.create_task
    directly and inspects the store at the exact moment it is invoked, which
    is what "written before the task is scheduled" actually claims. The
    coroutine-name filter keeps it from tripping on unrelated create_task
    calls the test client's own ASGI transport may make."""
    import asyncio as asyncio_module

    app = _app(tmp_path, monkeypatch, documents={"resume_text": "Ten years backend."})
    store = app.state.stores.wizard
    seen_at_schedule_time = []
    real_create_task = asyncio_module.create_task

    async def fake_run(app_, run_id):
        return {"status": "ok"}

    def spying_create_task(coro, *args, **kwargs):
        if getattr(getattr(coro, "cr_code", None), "co_name", None) == "fake_run":
            seen_at_schedule_time.append(store.get("content_draft"))
        return real_create_task(coro, *args, **kwargs)

    monkeypatch.setattr("src.web.settings.content_draft.run_content_draft", fake_run)
    monkeypatch.setattr("src.web.settings.content_draft.asyncio.create_task", spying_create_task)
    signed_in_client(app).post("/settings/documents/draft")
    assert len(seen_at_schedule_time) == 1
    seen = seen_at_schedule_time[0]
    assert seen["status"] == "running"
    # started_at has to be on this synchronous write too, not only on the
    # task's own: a process that dies between the two would otherwise leave
    # an unstamped record, which is the state _current has to age out.
    assert isinstance(seen["started_at"], float)


def test_an_existing_document_needs_confirmation_and_starts_no_task(tmp_path, monkeypatch):
    """Test 10 in the spec: no silent clobber."""
    app = _app(tmp_path, monkeypatch, documents={"resume_text": "CV",
                                                 "resume_content": CONTENT_JSON})
    tripped = False

    async def tripwire(app_, run_id):
        nonlocal tripped
        tripped = True

    monkeypatch.setattr("src.web.settings.content_draft.run_content_draft", tripwire)
    r = signed_in_client(app).post("/settings/documents/draft", follow_redirects=True)
    assert tripped is False
    assert "Replace it" in r.text
    assert app.state.service.snapshot().documents.resume_content == CONTENT_JSON


def test_confirming_starts_the_task(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, documents={"resume_text": "CV",
                                                 "resume_content": CONTENT_JSON})

    async def fake_run(app_, run_id):
        return {"status": "ok"}

    monkeypatch.setattr("src.web.settings.content_draft.run_content_draft", fake_run)
    signed_in_client(app).post("/settings/documents/draft", data={"overwrite": "1"})
    assert app.state.stores.wizard.get("content_draft")["status"] == "running"


def test_the_status_route_renders_the_stored_record(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    app.state.stores.wizard.put("content_draft",
                                {"status": "error", "error": "The LLM call failed (RuntimeError)."})
    r = signed_in_client(app).get("/settings/documents/draft/status")
    assert "The LLM call failed (RuntimeError)." in r.text
    assert "Edit as JSON instead" in r.text


def test_the_status_route_renders_the_ok_branch_too(tmp_path, monkeypatch):
    """The only other status-route test above hits the 'error' branch,
    which never reaches the {% import "_settings_macros.html" as w %} at
    the top of _content_draft_result.html — so deleting that import left a
    green suite despite a live UndefinedError on this route once a draft
    actually completes. Render the 'ok' branch through the status route
    itself (not the full settings page, which has its own import via
    settings_base.html and would mask the same regression)."""
    app = _app(tmp_path, monkeypatch, documents={"resume_text": "CV"})
    app.state.stores.wizard.put("content_draft", {
        "status": "ok", "document": DRAFTED,
        "contact": {"email": "s@e.com"},
    })
    r = signed_in_client(app).get("/settings/documents/draft/status")
    assert r.status_code == 200
    assert 'name="text.acme-b1"' in r.text


# asyncio_mode = "auto" (pyproject.toml:87) makes @pytest.mark.asyncio redundant
# on these; a previous task in this plan removed one for being inconsistent
# with sibling tests, so it's omitted here too.
async def test_the_task_stores_a_failure_rather_than_raising(tmp_path, monkeypatch):
    from src.resume_intake.errors import DraftFailed
    from src.web.settings.content_draft import claim_run, run_content_draft

    app = _app(tmp_path, monkeypatch, documents={"resume_text": "CV"})

    async def boom(cfg, *, resume_text):
        raise DraftFailed("no dice")

    monkeypatch.setattr("src.web.settings.content_draft.draft_content", boom)
    record = await run_content_draft(app, claim_run(app.state.stores.wizard))
    assert record["status"] == "error"
    assert "no dice" in record["error"]
    assert app.state.stores.wizard.get("content_draft")["status"] == "error"


async def test_the_task_stores_the_drafted_document(tmp_path, monkeypatch):
    from src.resume_intake.content_draft import ContentDraft
    from src.web.settings.content_draft import claim_run, run_content_draft

    app = _app(tmp_path, monkeypatch, documents={"resume_text": "CV"})
    document = {"name": "S", "skills": [], "experiences": [], "projects": [],
                "education": [], "volunteer": [], "contact": {"email": "s@e.com"}}

    async def ok(cfg, *, resume_text):
        return ContentDraft(document=document, contact={"email": "s@e.com"})

    monkeypatch.setattr("src.web.settings.content_draft.draft_content", ok)
    record = await run_content_draft(app, claim_run(app.state.stores.wizard))
    assert record["status"] == "ok"
    assert record["document"]["name"] == "S"
    assert record["contact"] == {"email": "s@e.com"}


async def test_the_task_never_writes_a_document_itself(tmp_path, monkeypatch):
    """Drafting proposes; only the review form's save writes. Otherwise the
    review gate is decorative."""
    from src.resume_intake.content_draft import ContentDraft
    from src.web.settings.content_draft import claim_run, run_content_draft

    app = _app(tmp_path, monkeypatch, documents={"resume_text": "CV"})

    async def ok(cfg, *, resume_text):
        return ContentDraft(document={"name": "S", "skills": [], "experiences": []},
                            contact={})

    monkeypatch.setattr("src.web.settings.content_draft.draft_content", ok)
    await run_content_draft(app, claim_run(app.state.stores.wizard))
    assert app.state.service.snapshot().documents.resume_content is None
    assert app.state.service.snapshot().documents.kit_facts is None


# --- apply_edits + the review form + the save route ---

DRAFTED = {
    "name": "S", "contact": {"email": "s@e.com"},
    "skills": [{"name": "Go", "category": "language", "tags": []},
               {"name": "Rust", "category": "language", "tags": []}],
    "experiences": [{
        "id": "acme", "company": "Acme", "role": "Staff",
        "start": "2021", "end": "now",
        "bullets": [
            {"id": "acme-b1", "text": "first", "tags": [], "metric_bearing": False,
             "evidence_refs": []},
            {"id": "acme-b2", "text": "second", "tags": [], "metric_bearing": False,
             "evidence_refs": []},
        ],
    }],
    "projects": [], "education": [{"degree": "BS", "institution": "UIUC", "dates": "2014"}],
    "volunteer": [],
}


def test_edits_change_text_and_drops_remove_bullets():
    """Spec test 11."""
    out = apply_edits(DRAFTED, {
        "text.acme-b1": ["edited first"],
        "text.acme-b2": ["second"],
        "drop.acme-b2": ["1"],
        "company.acme": ["Acme Corp"],
        "role.acme": ["Staff"],
        "start.acme": ["2021-03"],
        "end.acme": ["present"],
        "skills": ["Go", "Rust"],
    })
    bullets = out["experiences"][0]["bullets"]
    assert [b["id"] for b in bullets] == ["acme-b1"]
    assert bullets[0]["text"] == "edited first"
    assert out["experiences"][0]["company"] == "Acme Corp"
    assert out["experiences"][0]["start"] == "2021-03"


def test_ids_are_never_taken_from_the_form():
    """Structure comes from the stored draft. A post that invents an entry or
    rewrites an id must not be able to change the document's shape."""
    out = apply_edits(DRAFTED, {
        "text.acme-b1": ["kept"],
        "text.INVENTED": ["smuggled in"],
        "company.INVENTED": ["Nowhere Inc"],
        "skills": ["Go"],
    })
    assert [e["id"] for e in out["experiences"]] == ["acme"]
    # A positive check alongside the negative one below: proves the form WAS
    # applied (acme-b1's text changed from "first" to "kept"), so the
    # negative assertion can't be passing merely because apply_edits ignored
    # the form and echoed the stored draft back unchanged.
    assert out["experiences"][0]["bullets"][0]["text"] == "kept"
    assert "smuggled in" not in json.dumps(out)
    # Also rules out an implementation that invents a *project* (rather than
    # an experience) from company.INVENTED — that would still satisfy the
    # two assertions above, since neither looks at out["projects"].
    assert "Nowhere Inc" not in json.dumps(out)


def test_apply_edits_drops_an_entry_left_with_no_bullets():
    """Dropping (or blanking) every bullet under a role must remove that
    role from the result rather than write it with an empty bullet list —
    parse_content on its own accepts a bulletless experience, so this is the
    only thing that makes an empty entry structurally impossible to save."""
    out = apply_edits(DRAFTED, {
        "drop.acme-b1": ["1"], "drop.acme-b2": ["1"], "skills": ["Go"],
    })
    assert out["experiences"] == []


def test_metric_bearing_is_recomputed_from_the_edited_text():
    out = apply_edits(DRAFTED, {"text.acme-b1": ["Cut latency 40%"],
                                "text.acme-b2": ["no numbers here"], "skills": []})
    flags = [b["metric_bearing"] for b in out["experiences"][0]["bullets"]]
    assert flags == [True, False]


def test_dropped_skills_go_and_kept_skills_keep_their_category():
    out = apply_edits(DRAFTED, {"text.acme-b1": ["x"], "text.acme-b2": ["y"],
                                "skills": ["Go", "Postgres"]})
    assert [s["name"] for s in out["skills"]] == ["Go", "Postgres"]
    assert out["skills"][0]["category"] == "language"
    assert out["skills"][1]["category"] == ""


def test_sections_the_form_does_not_render_survive_unchanged():
    """Education and volunteer have no ids to address them by, so they are
    shown read-only and edited in the JSON editor. They must not be dropped."""
    out = apply_edits(DRAFTED, {"text.acme-b1": ["x"], "text.acme-b2": ["y"],
                                "skills": ["Go"]})
    assert out["education"] == DRAFTED["education"]
    assert out["contact"] == DRAFTED["contact"]


def test_the_result_still_passes_the_real_validator():
    from src.tailor.content import parse_content

    out = apply_edits(DRAFTED, {"text.acme-b1": ["x"], "drop.acme-b2": ["1"],
                                "skills": ["Go"]})
    assert len(parse_content(json.dumps(out)).experiences[0].bullets) == 1


# --- the save route ---

def _ready(app, document=None):
    app.state.stores.wizard.put("content_draft", {
        "status": "ok", "document": document or DRAFTED,
        "contact": {"email": "s@e.com", "github": "https://github.com/sc"},
    })
    return app


def test_the_review_form_renders_every_bullet_as_editable_text(tmp_path, monkeypatch):
    app = _ready(_app(tmp_path, monkeypatch, documents={"resume_text": "CV"}))
    r = signed_in_client(app).get("/settings/documents/draft")
    assert 'name="text.acme-b1"' in r.text
    assert 'name="drop.acme-b2"' in r.text
    assert "first" in r.text and "second" in r.text


def test_saving_writes_the_document_and_clears_the_draft(tmp_path, monkeypatch):
    app = _ready(_app(tmp_path, monkeypatch, documents={"resume_text": "CV"}))
    r = signed_in_client(app).post("/settings/documents/draft/save", data={
        "text.acme-b1": "first", "text.acme-b2": "second", "skills": "Go",
    }, follow_redirects=False)
    assert r.status_code == 303
    saved = app.state.service.snapshot().documents.resume_content
    assert saved is not None and "acme-b1" in saved
    assert app.state.stores.wizard.get("content_draft") is None


def test_saving_also_scaffolds_facts_when_there_are_none(tmp_path, monkeypatch):
    from src.kit_facts import parse_facts

    app = _ready(_app(tmp_path, monkeypatch, documents={"resume_text": "CV"}))
    signed_in_client(app).post("/settings/documents/draft/save",
                               data={"text.acme-b1": "first", "skills": "Go"})
    facts = app.state.service.snapshot().documents.kit_facts
    assert facts is not None
    assert [g.name for g in parse_facts(facts)] == ["Links", "Eligibility", "EEO"]


def test_saving_never_overwrites_existing_facts(tmp_path, monkeypatch):
    existing = "- group: Mine\n  facts:\n    - label: Keep\n      value: \"me\"\n"
    app = _ready(_app(tmp_path, monkeypatch, documents={"resume_text": "CV",
                                                        "kit_facts": existing}))
    signed_in_client(app).post("/settings/documents/draft/save",
                               data={"text.acme-b1": "first", "skills": "Go"})
    assert app.state.service.snapshot().documents.kit_facts == existing


def test_saving_with_no_draft_is_a_conflict_not_a_500(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, documents={"resume_text": "CV"})
    r = signed_in_client(app).post("/settings/documents/draft/save",
                                   data={"skills": "Go"})
    assert r.status_code == 409


def test_dropping_every_bullet_is_refused_and_the_draft_survives(tmp_path, monkeypatch):
    """parse_content happily accepts an experience with no bullets, so the
    validator will not catch this — a résumé of empty roles would be written
    silently and then tailored into a blank page. Refuse it here, keep the
    draft so the user can undo, and write nothing."""
    app = _ready(_app(tmp_path, monkeypatch, documents={"resume_text": "CV"}))
    r = signed_in_client(app).post("/settings/documents/draft/save", data={
        "drop.acme-b1": "1", "drop.acme-b2": "1", "skills": "Go",
    })
    assert r.status_code == 200
    assert "nothing to tailor" in r.text
    assert app.state.service.snapshot().documents.resume_content is None
    assert app.state.stores.wizard.get("content_draft")["status"] == "ok"


def test_a_nothing_left_refusal_re_renders_a_form_the_user_can_act_on(tmp_path, monkeypatch):
    """apply_edits drops an entry left with no bullets, so the edited
    document behind this refusal has zero experiences and zero projects by
    construction. Re-rendering THAT document (as SettingsInvalid correctly
    does) would show a page with no bullet fields at all — a dead end, since
    the dropped bullets are the very thing the message says to keep at
    least one of. The re-render must come from the stored draft instead, so
    every bullet comes back un-dropped and the user has something to act
    on in place, not just a message and a Save button."""
    app = _ready(_app(tmp_path, monkeypatch, documents={"resume_text": "CV"}))
    r = signed_in_client(app).post("/settings/documents/draft/save", data={
        "drop.acme-b1": "1", "drop.acme-b2": "1", "skills": "Go",
    })
    assert r.status_code == 200
    assert "nothing to tailor" in r.text
    assert 'name="text.acme-b1"' in r.text
    assert 'name="drop.acme-b2"' in r.text


TWO_ROLES = {
    **DRAFTED,
    "experiences": [
        DRAFTED["experiences"][0],
        {"id": "beta", "company": "Beta", "role": "Eng", "start": "2019", "end": "2021",
         "bullets": [{"id": "beta-b1", "text": "shipped x", "tags": [],
                      "metric_bearing": False, "evidence_refs": []}]},
    ],
}


def test_dropping_every_bullet_of_one_role_does_not_write_a_phantom_empty_role(tmp_path, monkeypatch):
    """The refusal used to be a document-wide check (any bullet anywhere?),
    which missed the case where one role is emptied while another keeps a
    bullet: that combination saved successfully and wrote a real role with
    zero bullets, which parse_content does not catch either. Emptying acme
    while beta keeps a bullet must save beta only — acme must not appear at
    all, let alone with an empty bullet list."""
    app = _ready(_app(tmp_path, monkeypatch, documents={"resume_text": "CV"}), TWO_ROLES)
    r = signed_in_client(app).post("/settings/documents/draft/save", data={
        "drop.acme-b1": "1", "drop.acme-b2": "1", "text.beta-b1": "shipped x", "skills": "Go",
    }, follow_redirects=False)
    assert r.status_code == 303
    saved = json.loads(app.state.service.snapshot().documents.resume_content)
    assert [e["id"] for e in saved["experiences"]] == ["beta"]
    assert all(e["bullets"] for e in saved["experiences"])


WITH_PROJECT = {
    **DRAFTED,
    "projects": [{"id": "proj1", "name": "Widget", "subtitle": "", "dates": "2022",
                 "bullets": [{"id": "proj1-b1", "text": "built widget", "tags": [],
                              "metric_bearing": False, "evidence_refs": []}]}],
}


def test_a_projects_only_result_is_allowed(tmp_path, monkeypatch):
    """A new grad with projects and no work history is a real résumé, not an
    empty one — the refusal only fires when experiences AND projects are
    both empty, not when either one alone is."""
    app = _ready(_app(tmp_path, monkeypatch, documents={"resume_text": "CV"}), WITH_PROJECT)
    r = signed_in_client(app).post("/settings/documents/draft/save", data={
        "drop.acme-b1": "1", "drop.acme-b2": "1", "text.proj1-b1": "built widget", "skills": "Go",
    }, follow_redirects=False)
    assert r.status_code == 303
    saved = json.loads(app.state.service.snapshot().documents.resume_content)
    assert saved["experiences"] == []
    assert [p["id"] for p in saved["projects"]] == ["proj1"]


MISSING_NAME = {k: v for k, v in DRAFTED.items() if k != "name"}


def test_a_refused_save_re_renders_with_the_edited_document_not_the_stored_draft(tmp_path, monkeypatch):
    """The spec's error table promises the form re-renders with the user's
    edits intact on a refusal. A SettingsInvalid from save_document (here:
    the stored draft is missing content.json's required 'name' key) must
    not fall back to redisplaying the last-stored draft, which would
    silently discard whatever the user just typed."""
    app = _ready(_app(tmp_path, monkeypatch, documents={"resume_text": "CV"}), MISSING_NAME)
    r = signed_in_client(app).post("/settings/documents/draft/save", data={
        "text.acme-b1": "first", "text.acme-b2": "second",
        "company.acme": "EDITED CO", "skills": "Go",
    })
    assert r.status_code == 200
    assert "EDITED CO" in r.text
    assert app.state.service.snapshot().documents.resume_content is None


def test_a_kit_facts_scaffold_failure_does_not_block_the_resume_save(tmp_path, monkeypatch):
    """The résumé write is the part that matters. A failure scaffolding
    kit_facts afterward (build_facts_yaml's output should always validate,
    but this guards the case it doesn't) must not turn an already-successful
    resume_content save into a 500 — the redirect must still happen."""
    monkeypatch.setattr("src.web.settings.content_draft.build_facts_yaml",
                        lambda contact, cfg: "not: [valid, yaml: structure")
    app = _ready(_app(tmp_path, monkeypatch, documents={"resume_text": "CV"}))
    r = signed_in_client(app).post("/settings/documents/draft/save", data={
        "text.acme-b1": "first", "text.acme-b2": "second", "skills": "Go",
    }, follow_redirects=False)
    assert r.status_code == 303
    snap = app.state.service.snapshot()
    assert snap.documents.resume_content is not None
    assert snap.documents.kit_facts is None


# --- M1: a stale draft must not silently replace a newer document ---

HAND_WRITTEN = """{"name": "Dana", "skills": [], "experiences": [
  {"id": "h1", "company": "Hand", "role": "Written",
   "bullets": [{"id": "h1-b1", "text": "typed by the user"}]}]}"""

SAVE_FORM = {"text.acme-b1": "first", "text.acme-b2": "second", "skills": "Go"}


def _digest(text):
    """Computed here rather than imported, so a change to how the route
    stamps a draft has to be a deliberate change to this test too."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _drafted_against(app, digest, document=None):
    """An ok draft record carrying the resume_content digest it was made
    against — exactly what run_content_draft stamps in production."""
    app.state.stores.wizard.put("content_draft", {
        "status": "ok", "document": document or DRAFTED,
        "contact": {"email": "s@e.com", "github": "https://github.com/sc"},
        "content_digest": digest,
    })
    return app


def _hand_write(app, body):
    """The page's own "Edit as JSON instead" escape hatch: the user leaves the
    review form open in one tab and writes resume_content by hand in another.
    That link is the shortest route into the state M1 describes."""
    app.state.service.save_document("resume_content", body, source="ui")


def test_a_draft_will_not_silently_replace_a_document_written_since(tmp_path, monkeypatch):
    """M1. The start POST's confirm gate cannot cover a document written
    after the draft was made, and this page links straight to the editor that
    writes one. Saving the stale review used to 303 and replace the
    hand-written document with no warning at all."""
    app = _drafted_against(_app(tmp_path, monkeypatch, documents={"resume_text": "CV"}),
                           NO_DOCUMENT)
    _hand_write(app, HAND_WRITTEN)
    r = signed_in_client(app).post("/settings/documents/draft/save", data=SAVE_FORM,
                                   follow_redirects=False)
    assert r.status_code == 200
    assert "has changed since" in r.text
    assert 'name="overwrite"' in r.text
    # The assertion that carries the requirement: the user's own work survives.
    assert app.state.service.snapshot().documents.resume_content == HAND_WRITTEN


def test_the_refused_save_keeps_the_users_edits_on_the_redisplayed_form(tmp_path, monkeypatch):
    """The refusal re-renders the edited document, not the stored draft: the
    typed text is valid input held back for an unrelated reason, so losing it
    would trade one silent data loss for a smaller one.

    follow_redirects=False and the 200 matter as much as the string does.
    Mutation testing caught this asserting nothing: with the gate removed the
    save succeeded, the client followed the 303 to the documents page, and
    "EDITED CO" was there — in the document that had just been written over
    the user's own. The status code is what distinguishes "your edit came
    back on the form" from "your edit was saved on top of someone's work"."""
    app = _drafted_against(_app(tmp_path, monkeypatch, documents={"resume_text": "CV"}),
                           NO_DOCUMENT)
    _hand_write(app, HAND_WRITTEN)
    r = signed_in_client(app).post("/settings/documents/draft/save", data={
        **SAVE_FORM, "company.acme": "EDITED CO"}, follow_redirects=False)
    assert r.status_code == 200
    assert "EDITED CO" in r.text
    assert app.state.service.snapshot().documents.resume_content == HAND_WRITTEN


def test_ticking_replace_it_lets_the_stale_draft_save(tmp_path, monkeypatch):
    """The gate is an opt-in, not a wall — same shape as the start gate and
    the settings import guard."""
    app = _drafted_against(_app(tmp_path, monkeypatch, documents={"resume_text": "CV"}),
                           NO_DOCUMENT)
    _hand_write(app, HAND_WRITTEN)
    r = signed_in_client(app).post("/settings/documents/draft/save",
                                   data={**SAVE_FORM, "overwrite": "1"},
                                   follow_redirects=False)
    assert r.status_code == 303
    saved = app.state.service.snapshot().documents.resume_content
    assert "acme-b1" in saved
    assert "typed by the user" not in saved


def test_a_draft_stamped_against_the_document_in_effect_needs_no_second_confirm(
        tmp_path, monkeypatch):
    """The overwhelmingly common case: the user ticked "Replace it" on the
    start gate and nothing has touched the document since. A second
    confirmation here would train them to tick past it without reading."""
    app = _app(tmp_path, monkeypatch, documents={"resume_text": "CV",
                                                 "resume_content": CONTENT_JSON})
    _drafted_against(app, _digest(CONTENT_JSON))
    r = signed_in_client(app).post("/settings/documents/draft/save", data=SAVE_FORM,
                                   follow_redirects=False)
    assert r.status_code == 303
    assert "acme-b1" in app.state.service.snapshot().documents.resume_content


def test_an_unstamped_draft_with_no_document_in_effect_saves_normally(tmp_path, monkeypatch):
    """A record written before the stamp existed (an in-flight draft across
    an upgrade) must not demand a confirmation naming a document the user
    does not have. The gate asks "would this replace something?", not "has
    the digest moved?"."""
    app = _ready(_app(tmp_path, monkeypatch, documents={"resume_text": "CV"}))
    assert "content_digest" not in app.state.stores.wizard.get("content_draft")
    r = signed_in_client(app).post("/settings/documents/draft/save", data=SAVE_FORM,
                                   follow_redirects=False)
    assert r.status_code == 303


async def test_the_task_stamps_the_record_with_the_document_it_drafted_against(
        tmp_path, monkeypatch):
    """Where the stamp comes from. Taken at the START of the call, so a
    document hand-written during the minute-plus generation is caught too."""
    from src.resume_intake.content_draft import ContentDraft
    from src.web.settings.content_draft import claim_run, run_content_draft

    app = _app(tmp_path, monkeypatch, documents={"resume_text": "CV",
                                                 "resume_content": CONTENT_JSON})

    async def ok(cfg, *, resume_text):
        return ContentDraft(document=DRAFTED, contact={})

    monkeypatch.setattr("src.web.settings.content_draft.draft_content", ok)
    record = await run_content_draft(app, claim_run(app.state.stores.wizard))
    assert record["content_digest"] == _digest(CONTENT_JSON)
    assert record["started_at"] > 0
    stored = app.state.stores.wizard.get("content_draft")
    assert stored["content_digest"] == _digest(CONTENT_JSON)


# --- M2: a running record that never finishes must not brick the feature ---

def _running(app, age_seconds=0.0):
    app.state.stores.wizard.put("content_draft", {
        "status": "running", "started_at": time.time() - age_seconds})
    return app


def test_a_fresh_running_draft_still_polls(tmp_path, monkeypatch):
    app = _running(_app(tmp_path, monkeypatch, documents={"resume_text": "CV"}))
    body = signed_in_client(app).get("/settings/documents/draft/status").text
    assert 'hx-trigger="load delay:2s"' in body
    assert "never finished" not in body


def test_a_running_draft_past_the_bound_becomes_a_recoverable_error(tmp_path, monkeypatch):
    """M2. A process exit during the call (docker restart, --build, reload)
    left a "running" record that nothing would ever replace, and the running
    branch rendered a poll with no form and no button — so both entry points
    led to a page that polls forever, recoverable only by editing the
    wizard_ui table by hand."""
    app = _running(_app(tmp_path, monkeypatch, documents={"resume_text": "CV"}),
                   age_seconds=3600)
    body = signed_in_client(app).get("/settings/documents/draft").text
    assert "never finished" in body
    assert "Try again" in body
    assert 'hx-trigger="load delay:2s"' not in body
    assert app.state.service.snapshot().documents.resume_content is None


def test_a_running_record_with_no_timestamp_is_recoverable_too(tmp_path, monkeypatch):
    """An unstamped record was written before this check existed, which is
    precisely the stuck state it recovers — so it must age out rather than
    read as brand new and poll forever."""
    app = _app(tmp_path, monkeypatch, documents={"resume_text": "CV"})
    app.state.stores.wizard.put("content_draft", {"status": "running"})
    assert "never finished" in signed_in_client(app).get("/settings/documents/draft").text


def test_the_staleness_check_presents_without_rewriting_the_record(tmp_path, monkeypatch):
    """A GET must not write, and a task that somehow is still alive still
    owns the key — it has to be able to land its real result on it."""
    app = _running(_app(tmp_path, monkeypatch, documents={"resume_text": "CV"}),
                   age_seconds=3600)
    signed_in_client(app).get("/settings/documents/draft")
    assert app.state.stores.wizard.get("content_draft")["status"] == "running"


def test_the_staleness_bound_never_undercuts_the_drafting_calls_own_timeout():
    """A bound below the call's effective timeout would declare a live draft
    dead and invite the user to start a second one racing the first. The call
    floors its timeout at MIN_TIMEOUT_SECONDS and otherwise honours the
    setting, so the bound has to track both — not a bare constant."""
    from src.config import AppConfig
    from src.resume_intake.content_draft import MIN_TIMEOUT_SECONDS
    from src.web.settings.content_draft import _stale_after_seconds

    cfg = AppConfig()
    assert _stale_after_seconds(cfg) > MIN_TIMEOUT_SECONDS
    patient = cfg.model_copy(update={
        "resume_draft": cfg.resume_draft.model_copy(update={"timeout_seconds": 900})})
    assert _stale_after_seconds(patient) > 900


def test_the_running_page_offers_a_way_out(tmp_path, monkeypatch):
    """The running branch had no form and no button at all."""
    app = _running(_app(tmp_path, monkeypatch, documents={"resume_text": "CV"}))
    body = signed_in_client(app).get("/settings/documents/draft/status").text
    assert 'action="/settings/documents/draft/discard"' in body
    # The poll has to swap the button away together with the message. With the
    # hx attributes on the paragraph rather than the wrapper, an outerHTML
    # swap would leave a stale Start-over button beside the review form —
    # and with no wrapper at all there is no "</div>" to find, which fails
    # this the same way.
    assert body.index("draft/discard") < body.index("</div>")


def test_discarding_clears_the_draft_and_writes_nothing(tmp_path, monkeypatch):
    app = _ready(_app(tmp_path, monkeypatch, documents={"resume_text": "CV"}))
    r = signed_in_client(app).post("/settings/documents/draft/discard",
                                   follow_redirects=False)
    assert r.status_code == 303
    assert app.state.stores.wizard.get("content_draft") is None
    assert app.state.service.snapshot().documents.resume_content is None


def test_the_review_form_offers_start_over_outside_the_save_form(tmp_path, monkeypatch):
    """The ok branch was a one-way door: save this draft, or go hand-edit
    JSON. The discard form has to sit outside the save form — HTML has no
    nested forms, and a nested one would post nothing."""
    app = _ready(_app(tmp_path, monkeypatch, documents={"resume_text": "CV"}))
    body = signed_in_client(app).get("/settings/documents/draft/status").text
    assert 'action="/settings/documents/draft/discard"' in body
    assert body.index("</form>") < body.index("draft/discard")


# --- the error branch's retry, and saving into a running draft ---

def test_the_retry_after_an_error_does_not_carry_the_overwrite_opt_in(tmp_path, monkeypatch):
    """It shipped overwrite=1 as a hidden field, so one error bypassed the
    confirm gate for the rest of that record's life — and would bypass M1's
    save-time gate in exactly the same way."""
    app = _app(tmp_path, monkeypatch, documents={"resume_text": "CV"})
    app.state.stores.wizard.put("content_draft", {"status": "error", "error": "boom"})
    body = signed_in_client(app).get("/settings/documents/draft/status").text
    assert "Try again" in body
    assert 'name="overwrite"' not in body


def test_saving_while_a_newer_draft_runs_explains_instead_of_dumping_json(tmp_path, monkeypatch):
    """It returned a raw {"detail": "no draft to save"} 409, which loses the
    page along with the typed edits. The edits cannot be kept — the draft
    they were edits to is gone — but the reason can be said out loud."""
    app = _running(_app(tmp_path, monkeypatch, documents={"resume_text": "CV"}))
    r = signed_in_client(app).post("/settings/documents/draft/save", data=SAVE_FORM)
    assert r.status_code == 409
    assert "no longer the current one" in r.text
    assert "no draft to save" not in r.text


# --- blanking the fields an entry is named by ---

def test_apply_edits_drops_an_entry_whose_company_and_role_are_both_blanked():
    """assign_ids drops a drafted entry with neither; the review form has to
    agree, or blanking both writes a nameless role with real bullets under an
    empty heading. parse_content accepts two empty strings."""
    out = apply_edits(DRAFTED, {"text.acme-b1": ["kept"], "company.acme": [""],
                                "role.acme": [""], "skills": ["Go"]})
    assert out["experiences"] == []


def test_blanking_only_one_of_company_and_role_keeps_the_entry():
    """A contractor with no company name, or a role the user cannot recall,
    is still a real entry. Only losing both makes it nameless."""
    out = apply_edits(DRAFTED, {"text.acme-b1": ["kept"], "company.acme": [""],
                                "role.acme": ["Staff"], "skills": ["Go"]})
    assert [e["role"] for e in out["experiences"]] == ["Staff"]
    assert out["experiences"][0]["company"] == ""


def test_apply_edits_drops_a_project_whose_name_is_blanked():
    """A project's name is the only heading it has, and assign_ids requires
    one."""
    out = apply_edits(WITH_PROJECT, {"text.acme-b1": ["kept"], "text.proj1-b1": ["built"],
                                     "name.proj1": [""], "skills": ["Go"]})
    assert out["projects"] == []
    assert [e["id"] for e in out["experiences"]] == ["acme"]


def test_saving_a_blanked_company_and_role_writes_no_nameless_entry(tmp_path, monkeypatch):
    app = _ready(_app(tmp_path, monkeypatch, documents={"resume_text": "CV"}), TWO_ROLES)
    r = signed_in_client(app).post("/settings/documents/draft/save", data={
        "company.acme": "", "role.acme": "", "text.acme-b1": "first",
        "text.beta-b1": "shipped x", "skills": "Go",
    }, follow_redirects=False)
    assert r.status_code == 303
    saved = json.loads(app.state.service.snapshot().documents.resume_content)
    assert [e["id"] for e in saved["experiences"]] == ["beta"]


# --- what the review gate shows ---

TRANSCRIBED_CONTACT = {
    **DRAFTED,
    "name": "Dana Example",
    "contact": {"email": "dana@example.com", "phone": "+1 555 0100",
                "location": "", "github": "https://github.com/dana",
                "linkedin": "", "website": ""},
    "education": [], "volunteer": [],
}


def test_the_review_page_shows_the_name_and_contact_it_transcribed(tmp_path, monkeypatch):
    """Both ship: the contact block becomes the rendered PDF's header, and
    the apply kit's Links/Email/Phone — what the autofill bookmarklet types
    into application forms — are scaffolded from it. A page that says "this
    is what your résumé says, correct it now" cannot hide how an employer
    reaches the user.

    This document has no education and no volunteer entries, which is what
    used to hide the whole panel."""
    app = _ready(_app(tmp_path, monkeypatch, documents={"resume_text": "CV"}),
                 TRANSCRIBED_CONTACT)
    body = signed_in_client(app).get("/settings/documents/draft/status").text
    assert "Dana Example" in body
    assert "dana@example.com" in body
    assert "+1 555 0100" in body
    assert "https://github.com/dana" in body
    # Read-only, per the spec's reasoning: a URL the model copied is a string
    # it transcribed, not a claim it made, so it needs showing rather than
    # approving — and the save route never has to trust a contact field.
    assert 'value="dana@example.com"' not in body
    # A blank field is omitted, not rendered as a bare label.
    assert "LinkedIn" not in body


# --- one draft at a time: a run owns the record, and only while it still does ---

def _async_client(app):
    """An httpx client on the test's own event loop, so a task a route
    schedules is one this test can see, await and inspect."""
    import httpx

    from tests.auth_helpers import sign_in

    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                               base_url="http://testserver")
    client.app = app  # sign_in reads the auth store off client.app
    return sign_in(client)


async def _hang(cfg, *, resume_text):
    import asyncio
    await asyncio.sleep(3600)


async def _settled(task):
    """Wait for ``task`` to finish, but fail rather than sit out _hang's hour
    when nothing cancelled it."""
    import asyncio
    await asyncio.wait({task}, timeout=5)
    assert task.done(), "the task was never cancelled"


def test_a_second_start_while_one_runs_starts_no_task(tmp_path, monkeypatch):
    """A second tab, or a double-submit, would otherwise start a second draft
    racing the first for the same record — last writer wins, and the review
    the user is reading can be swapped out from under them."""
    app = _running(_app(tmp_path, monkeypatch, documents={"resume_text": "CV"}))
    before = app.state.stores.wizard.get("content_draft")
    tripped = False

    async def tripwire(app_, run_id):
        nonlocal tripped
        tripped = True

    monkeypatch.setattr("src.web.settings.content_draft.run_content_draft", tripwire)
    r = signed_in_client(app).post("/settings/documents/draft", follow_redirects=True)
    assert tripped is False
    assert app.state.stores.wizard.get("content_draft") == before
    assert 'hx-trigger="load delay:2s"' in r.text  # back on the live poll


def test_a_start_after_the_staleness_bound_is_allowed(tmp_path, monkeypatch):
    """The refusal must not undo M2: a record past the bound is presented as
    an error whose "Try again" has to work."""
    app = _running(_app(tmp_path, monkeypatch, documents={"resume_text": "CV"}),
                   age_seconds=3600)
    tripped = False

    async def tripwire(app_, run_id):
        nonlocal tripped
        tripped = True

    monkeypatch.setattr("src.web.settings.content_draft.run_content_draft", tripwire)
    signed_in_client(app).post("/settings/documents/draft")
    assert tripped is True


async def test_a_discarded_run_does_not_bring_its_draft_back(tmp_path, monkeypatch):
    """Discarding mid-call used to delete the record, then the still-running
    task wrote its result on top: the draft the user threw away reappeared."""
    from src.resume_intake.content_draft import ContentDraft
    from src.web.settings.content_draft import claim_run, run_content_draft

    app = _app(tmp_path, monkeypatch, documents={"resume_text": "CV"})
    store = app.state.stores.wizard

    async def discarded_mid_call(cfg, *, resume_text):
        store.delete("content_draft")
        return ContentDraft(document=DRAFTED, contact={})

    monkeypatch.setattr("src.web.settings.content_draft.draft_content", discarded_mid_call)
    await run_content_draft(app, claim_run(store))
    assert store.get("content_draft") is None


async def test_a_superseded_run_does_not_overwrite_the_newer_one(tmp_path, monkeypatch):
    from src.resume_intake.content_draft import ContentDraft
    from src.web.settings.content_draft import claim_run, run_content_draft

    app = _app(tmp_path, monkeypatch, documents={"resume_text": "CV"})
    store = app.state.stores.wizard
    newer = {}

    async def superseded_mid_call(cfg, *, resume_text):
        newer["run_id"] = claim_run(store)
        return ContentDraft(document=DRAFTED, contact={})

    monkeypatch.setattr("src.web.settings.content_draft.draft_content", superseded_mid_call)
    await run_content_draft(app, claim_run(store))
    record = store.get("content_draft")
    assert record["status"] == "running"
    assert record["run_id"] == newer["run_id"]


async def test_discarding_cancels_the_running_task(tmp_path, monkeypatch):
    """Ownership stops a discarded run from writing; cancelling it also stops
    it spending another minute of LLM time on a draft nobody will read."""
    import asyncio

    app = _app(tmp_path, monkeypatch, documents={"resume_text": "CV"})
    monkeypatch.setattr("src.web.settings.content_draft.draft_content", _hang)
    async with _async_client(app) as client:
        await client.post("/settings/documents/draft")
        task = app.state.content_draft_task
        await asyncio.sleep(0)  # let it reach the provider call
        assert not task.done()
        await client.post("/settings/documents/draft/discard")
        try:
            await _settled(task)
            assert task.cancelled()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert app.state.stores.wizard.get("content_draft") is None


async def test_restarting_a_presumed_dead_draft_cancels_it_if_it_was_alive(
        tmp_path, monkeypatch):
    """Past the staleness bound the page offers "Try again", but the old task
    may be merely slow rather than dead. The new start replaces it."""
    import asyncio

    app = _app(tmp_path, monkeypatch, documents={"resume_text": "CV"})
    store = app.state.stores.wizard
    monkeypatch.setattr("src.web.settings.content_draft.draft_content", _hang)
    async with _async_client(app) as client:
        await client.post("/settings/documents/draft")
        old = app.state.content_draft_task
        await asyncio.sleep(0)
        store.put("content_draft", {**store.get("content_draft"), "started_at": 0.0})
        await client.post("/settings/documents/draft")
        new = app.state.content_draft_task
        try:
            await _settled(old)
            assert old.cancelled()
            assert new is not old and not new.done()
        finally:
            old.cancel()
            new.cancel()
            await asyncio.gather(old, new, return_exceptions=True)


def test_saving_a_draft_that_never_finished_explains_instead_of_dumping_json(
        tmp_path, monkeypatch):
    """A running record past the bound is presented as an error, so the save
    route's "not ok" branch used to answer with a raw {"detail": ...} 409."""
    app = _running(_app(tmp_path, monkeypatch, documents={"resume_text": "CV"}),
                   age_seconds=3600)
    r = signed_in_client(app).post("/settings/documents/draft/save", data=SAVE_FORM)
    assert r.status_code == 409
    assert "Nothing was saved" in r.text
    assert "never finished" in r.text  # the page's own recovery options
    assert '"detail"' not in r.text
