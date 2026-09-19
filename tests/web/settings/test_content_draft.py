"""The drafting page: a background task, a polled status, and a confirm gate
in front of an existing document."""
import json

from src.web.app import create_app
from src.web.settings.content_draft import apply_edits
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


def test_starting_without_a_resume_records_an_error_and_starts_no_task(tmp_path, monkeypatch):
    from src.resume_intake.content_draft import NO_RESUME

    app = _app(tmp_path, monkeypatch)
    tripped = False

    async def tripwire(app_):
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

    async def fake_run(app_):
        return {"status": "ok"}

    def spying_create_task(coro, *args, **kwargs):
        if getattr(getattr(coro, "cr_code", None), "co_name", None) == "fake_run":
            seen_at_schedule_time.append(store.get("content_draft"))
        return real_create_task(coro, *args, **kwargs)

    monkeypatch.setattr("src.web.settings.content_draft.run_content_draft", fake_run)
    monkeypatch.setattr("src.web.settings.content_draft.asyncio.create_task", spying_create_task)
    signed_in_client(app).post("/settings/documents/draft")
    assert seen_at_schedule_time == [{"status": "running"}]


def test_an_existing_document_needs_confirmation_and_starts_no_task(tmp_path, monkeypatch):
    """Test 10 in the spec: no silent clobber."""
    app = _app(tmp_path, monkeypatch, documents={"resume_text": "CV",
                                                 "resume_content": CONTENT_JSON})
    tripped = False

    async def tripwire(app_):
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

    async def fake_run(app_):
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
    from src.resume_intake.draft import DraftFailed
    from src.web.settings.content_draft import run_content_draft

    app = _app(tmp_path, monkeypatch, documents={"resume_text": "CV"})

    async def boom(cfg, *, resume_text):
        raise DraftFailed("no dice")

    monkeypatch.setattr("src.web.settings.content_draft.draft_content", boom)
    record = await run_content_draft(app)
    assert record["status"] == "error"
    assert "no dice" in record["error"]
    assert app.state.stores.wizard.get("content_draft")["status"] == "error"


async def test_the_task_stores_the_drafted_document(tmp_path, monkeypatch):
    from src.resume_intake.content_draft import ContentDraft
    from src.web.settings.content_draft import run_content_draft

    app = _app(tmp_path, monkeypatch, documents={"resume_text": "CV"})
    document = {"name": "S", "skills": [], "experiences": [], "projects": [],
                "education": [], "volunteer": [], "contact": {"email": "s@e.com"}}

    async def ok(cfg, *, resume_text):
        return ContentDraft(document=document, contact={"email": "s@e.com"})

    monkeypatch.setattr("src.web.settings.content_draft.draft_content", ok)
    record = await run_content_draft(app)
    assert record["status"] == "ok"
    assert record["document"]["name"] == "S"
    assert record["contact"] == {"email": "s@e.com"}


async def test_the_task_never_writes_a_document_itself(tmp_path, monkeypatch):
    """Drafting proposes; only the review form's save writes. Otherwise the
    review gate is decorative."""
    from src.resume_intake.content_draft import ContentDraft
    from src.web.settings.content_draft import run_content_draft

    app = _app(tmp_path, monkeypatch, documents={"resume_text": "CV"})

    async def ok(cfg, *, resume_text):
        return ContentDraft(document={"name": "S", "skills": [], "experiences": []},
                            contact={})

    monkeypatch.setattr("src.web.settings.content_draft.draft_content", ok)
    await run_content_draft(app)
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
