"""The drafting page: a background task, a polled status, and a confirm gate
in front of an existing document."""
from src.web.app import create_app
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
