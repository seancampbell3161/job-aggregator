"""Review renders the draft as ordinary settings widgets — approving is a
normal save_bundle, not a special accept-the-LLM path."""
import pytest

from src.resume_intake.draft import Draft
from src.resume_intake.errors import DraftFailed
from src.web.app import create_app
from tests.auth_helpers import signed_in_client
from tests.settings_helpers import WEB_TEST_SETTINGS, make_service

DRAFT = Draft(
    profile_md="## Quick summary\nPlatform engineer.",
    filters={"filters.titles": ["platform engineer"], "filters.max_age_days": 2},
)


def _app(tmp_path, monkeypatch, documents=None):
    monkeypatch.setenv("JOB_AGG_SQLITE_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("JOB_AGG_TAILORED_DIR", str(tmp_path / "tailored"))
    return create_app(service=make_service(
        WEB_TEST_SETTINGS, documents=documents or {"resume_text": "ten years of python"}))


def _drafts(monkeypatch, result):
    async def fake(cfg, *, resume_text, answers):
        if isinstance(result, Exception):
            raise result
        return result
    monkeypatch.setattr("src.web.wizard.routes.draft_profile_and_filters", fake)


def test_first_visit_generates_and_shows_the_draft(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    _drafts(monkeypatch, DRAFT)
    r = signed_in_client(app).get("/wizard/review")
    assert r.status_code == 200
    assert "platform engineer" in r.text
    assert "Platform engineer." in r.text


def test_the_draft_is_persisted_so_a_reload_does_not_recall_the_llm(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    calls = []

    async def fake(cfg, *, resume_text, answers):
        calls.append(1)
        return DRAFT

    monkeypatch.setattr("src.web.wizard.routes.draft_profile_and_filters", fake)
    client = signed_in_client(app)
    client.get("/wizard/review")
    client.get("/wizard/review")
    assert len(calls) == 1


def test_redraft_calls_the_llm_again(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    calls = []

    async def fake(cfg, *, resume_text, answers):
        calls.append(1)
        return DRAFT

    monkeypatch.setattr("src.web.wizard.routes.draft_profile_and_filters", fake)
    client = signed_in_client(app)
    client.get("/wizard/review")
    client.post("/wizard/review/redraft")
    assert len(calls) == 2


def test_draft_again_is_offered_before_a_profile_is_saved(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    _drafts(monkeypatch, DRAFT)
    r = signed_in_client(app).get("/wizard/review")
    assert "Draft again" in r.text


def test_draft_again_is_hidden_once_a_profile_is_saved(tmp_path, monkeypatch):
    """Minor (whole-branch review): _ensure_draft() stops drafting once a
    profile document exists (the config is the current truth from then on),
    so the button used to render unconditionally and do nothing once
    clicked. It must not appear once there is nothing left for it to do."""
    app = _app(tmp_path, monkeypatch)
    _drafts(monkeypatch, DRAFT)
    client = signed_in_client(app)
    client.get("/wizard/review")
    client.post("/wizard/review", data={
        "profile": "## Quick summary\nSaved already.",
        "filters.titles": ["platform engineer"],
        "filters.max_age_days": "2",
        "filters.location.allowed_countries": ["US"],
        "filters.location.remote_policy": "allowed_countries",
    })
    r = client.get("/wizard/review")
    assert "Draft again" not in r.text


def test_approving_writes_profile_and_filters_together(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    _drafts(monkeypatch, DRAFT)
    signed_in_client(app).get("/wizard/review")
    r = signed_in_client(app).post("/wizard/review", data={
        "profile": "## Quick summary\nEdited by hand.",
        "filters.titles": ["platform engineer"],
        "filters.seniority_allow": ["senior"],
        "filters.stack_any_of": [""],
        "filters.comp_floor_usd": "0",
        "filters.max_age_days": "2",
        "filters.blocked_employment_types": ["contract"],
        "filters.blocked_companies": [""],
        "filters.location.allowed_countries": ["US"],
        "filters.location.allowed_cities": [""],
        "filters.location.remote_policy": "allowed_countries",
        "filters.location.allow_unknown": "on",
    }, follow_redirects=False)
    assert r.status_code == 303
    snap = app.state.service.snapshot()
    assert snap.cfg.filters.titles == ["platform engineer"]
    assert "Edited by hand" in snap.documents.profile


def test_approving_a_drafted_version_is_sourced_llm_draft(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    _drafts(monkeypatch, DRAFT)
    client = signed_in_client(app)
    client.get("/wizard/review")
    client.post("/wizard/review", data={
        "profile": "## Quick summary\nx", "filters.titles": ["a"],
        "filters.max_age_days": "2", "filters.location.allowed_countries": ["US"],
        "filters.location.remote_policy": "allowed_countries",
    })
    assert app.state.service.versions(1)[0].source == "llm_draft"


def test_a_failed_draft_falls_back_to_manual_forms(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    _drafts(monkeypatch, DraftFailed("The LLM call failed (RuntimeError)."))
    r = signed_in_client(app).get("/wizard/review")
    assert r.status_code == 200
    assert "LLM call failed" in r.text
    assert 'name="filters.titles"' in r.text  # the form is still usable


def test_no_llm_still_renders_the_form(tmp_path, monkeypatch):
    """Nothing about this step requires an LLM."""
    app = _app(tmp_path, monkeypatch)
    _drafts(monkeypatch, DraftFailed("No LLM is connected"))
    r = signed_in_client(app).get("/wizard/review")
    assert 'name="profile"' in r.text


def test_without_a_draft_the_form_is_prefilled_from_the_interview(tmp_path, monkeypatch):
    """The interview answers already bind to real config paths, so a user who
    skipped the LLM should still find the filters filled in — otherwise the
    eight questions they just answered bought them nothing."""
    app = _app(tmp_path, monkeypatch)
    app.state.stores.wizard.put("answers", {
        "target_titles": ["platform engineer"],
        "comp_floor": ["185000"],
        "employment_types": ["full_time"],
    })
    _drafts(monkeypatch, DraftFailed("No LLM is connected"))
    r = signed_in_client(app).get("/wizard/review")
    assert "platform engineer" in r.text
    assert "185000" in r.text


def test_a_draft_wins_over_the_interview_prefill(tmp_path, monkeypatch):
    """The draft was built FROM those answers, so it is the later word."""
    app = _app(tmp_path, monkeypatch)
    app.state.stores.wizard.put("answers", {"target_titles": ["from interview"]})
    _drafts(monkeypatch, DRAFT)
    r = signed_in_client(app).get("/wizard/review")
    assert "platform engineer" in r.text
    assert "from interview" not in r.text


def test_a_draft_with_no_usable_filters_still_prefills_from_the_interview(
        tmp_path, monkeypatch):
    """_sanitize() drops every path a model proposed when it answers with bare
    keys (`titles` instead of `filters.titles`) — yet the draft is still "ok",
    because its profile came back fine. The interview answers are then the only
    word left about the filters, so they must not be thrown away along with the
    model's unusable paths, leaving the user with bare defaults."""
    app = _app(tmp_path, monkeypatch)
    app.state.stores.wizard.put("answers", {
        "target_titles": ["platform engineer"],
        "comp_floor": ["185000"],
    })
    _drafts(monkeypatch, Draft(
        profile_md="## Quick summary\nPlatform engineer.",
        filters={},
        warnings=["Ignored titles: the draft may only set filters."],
    ))
    r = signed_in_client(app).get("/wizard/review")
    assert "platform engineer" in r.text
    assert "185000" in r.text


def test_answers_survive_for_the_paths_a_draft_did_not_set(tmp_path, monkeypatch):
    """_sanitize() can keep one proposed path and drop another, so a draft is
    often partial. The paths it did set are the later word; for every path it
    did not, the interview is still the only thing the user told us."""
    app = _app(tmp_path, monkeypatch)
    app.state.stores.wizard.put("answers", {
        "target_titles": ["from interview"],
        "comp_floor": ["185000"],
    })
    _drafts(monkeypatch, Draft(
        profile_md="## Quick summary\nx",
        filters={"filters.titles": ["platform engineer"]},
    ))
    r = signed_in_client(app).get("/wizard/review")
    assert "platform engineer" in r.text
    assert "from interview" not in r.text
    assert "185000" in r.text


def test_warnings_are_shown(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    _drafts(monkeypatch, Draft(
        profile_md="## Quick summary\nx", filters={},
        warnings=["Ignored relevance.provider: the draft may only set filters."],
    ))
    r = signed_in_client(app).get("/wizard/review")
    assert "may only set filters" in r.text


def test_an_invalid_edit_re_renders_without_writing(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    _drafts(monkeypatch, DRAFT)
    client = signed_in_client(app)
    client.get("/wizard/review")
    r = client.post("/wizard/review", data={
        "profile": "## x", "filters.titles": ["a"], "filters.max_age_days": "later",
    })
    assert r.status_code == 200
    assert app.state.service.snapshot().documents.profile is None


def test_an_empty_profile_is_rejected(tmp_path, monkeypatch):
    """validate_document requires a non-blank profile."""
    app = _app(tmp_path, monkeypatch)
    _drafts(monkeypatch, DRAFT)
    client = signed_in_client(app)
    client.get("/wizard/review")
    r = client.post("/wizard/review", data={"profile": "   ", "filters.titles": ["a"]})
    assert r.status_code == 200
    assert app.state.service.snapshot().documents.profile is None


def test_blank_titles_leave_the_step_incomplete_and_explain_why(tmp_path, monkeypatch):
    """Important 2 (whole-branch review): save_step's sibling here always
    303s to /wizard, which re-picks the first incomplete step -- so a save
    that IS valid (an empty title list is a valid, if useless, config; decode
    and validate_document both pass, and the profile document really is
    written) but leaves review incomplete used to silently re-serve this
    page with no explanation, even though readiness.check() already has the
    exact text (no_titles) to explain it. Follow the redirect the way a
    browser would -- a bare status/location check on the POST can't see
    whether the warning text ever reached the page that's actually shown."""
    app = _app(tmp_path, monkeypatch, documents={})
    client = signed_in_client(app)
    # /wizard always routes to the first INCOMPLETE, unskipped step -- llm
    # and resume must be out of the way first, or the redirect below lands
    # back on one of those instead of review, and ?attempted=review never
    # matches what /wizard actually recomputes.
    client.post("/wizard/llm/skip")
    client.post("/wizard/resume/skip")
    r = client.post("/wizard/review", data={
        "profile": "## Quick summary\nSomething.",
        "filters.titles": [""],
        "filters.max_age_days": "3",
    })
    assert r.status_code == 200
    assert str(r.url).endswith("/wizard/review?attempted=1")
    assert "no job titles are set" in r.text.lower()
    # This is not the decode/validate_document failure path -- the save DID
    # happen, the step is just still incomplete.
    assert app.state.service.snapshot().documents.profile is not None


def test_a_first_unattempted_visit_to_review_shows_no_warning(tmp_path, monkeypatch):
    """Keep it honest: an instance that's already incomplete for reasons
    unrelated to this visit (no résumé, so no draft, nothing posted yet)
    gets no warning on a plain first GET."""
    app = _app(tmp_path, monkeypatch, documents={})
    r = signed_in_client(app).get("/wizard/review")
    assert r.status_code == 200
    assert "no job titles are set" not in r.text.lower()


def test_approving_clears_the_draft_so_a_revisit_shows_what_was_saved(tmp_path, monkeypatch):
    """A saved approval consumes the draft. Without this, review_shown()'s
    prefill layer would keep outranking the config on every later visit to
    this page, so an edit made at approval time (here: a different title than
    the draft proposed) would appear to silently revert on reload — the page
    would no longer behave like an ordinary settings page that shows what is
    actually saved."""
    app = _app(tmp_path, monkeypatch)
    _drafts(monkeypatch, DRAFT)  # drafts "platform engineer"
    client = signed_in_client(app)
    client.get("/wizard/review")
    client.post("/wizard/review", data={
        "profile": "## Quick summary\nEdited by hand.",
        "filters.titles": ["staff engineer"],  # the user typed something else
        "filters.max_age_days": "2",
        "filters.location.allowed_countries": ["US"],
        "filters.location.remote_policy": "allowed_countries",
    })
    r = client.get("/wizard/review")
    assert "staff engineer" in r.text
    assert "platform engineer" not in r.text
