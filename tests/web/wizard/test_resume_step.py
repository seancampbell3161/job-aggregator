"""Uploading a résumé writes the resume_text document and remembers the
interview answers for the review step."""
import pytest

from src.web.app import create_app
from tests.auth_helpers import signed_in_client
from tests.settings_helpers import WEB_TEST_SETTINGS, make_service

LONG = ("Platform engineer with ten years of Python and Kubernetes. " * 8)

ANSWERS = {
    "target_titles": "platform engineer",
    "seniority": "senior",
    "ic_or_management": "ic",
    "countries": "US",
    "cities": "",
    "remote_policy": "allowed_countries",
    "employment_types": "full_time",
    "comp_floor": "185000",
}


def _app(tmp_path, monkeypatch):
    monkeypatch.setenv("JOB_AGG_SQLITE_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("JOB_AGG_TAILORED_DIR", str(tmp_path / "tailored"))
    return create_app(service=make_service(WEB_TEST_SETTINGS))


def test_pasted_text_is_saved_as_the_resume_document(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    r = signed_in_client(app).post(
        "/wizard/resume", data={**ANSWERS, "pasted": LONG}, follow_redirects=False,
    )
    assert r.status_code == 303
    assert LONG.strip()[:40] in app.state.service.snapshot().documents.resume_text


def test_an_uploaded_file_is_saved(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    r = signed_in_client(app).post(
        "/wizard/resume", data=ANSWERS,
        files={"upload": ("r.txt", LONG.encode(), "text/plain")},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert app.state.service.snapshot().documents.resume_text is not None


def test_answers_are_remembered_for_the_review_step(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    signed_in_client(app).post("/wizard/resume", data={**ANSWERS, "pasted": LONG})
    stored = app.state.stores.wizard.get("answers")
    assert stored["target_titles"] == ["platform engineer"]
    assert stored["comp_floor"] == ["185000"]


def test_a_scan_re_renders_with_the_paste_hint(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    r = signed_in_client(app).post(
        "/wizard/resume", data=ANSWERS,
        files={"upload": ("r.txt", b"Bob", "text/plain")},
    )
    assert r.status_code == 200
    assert "paste" in r.text.lower()
    assert app.state.service.snapshot().documents.resume_text is None


def test_a_short_paste_is_echoed_back_on_the_error_rerender(tmp_path, monkeypatch):
    """Minor (whole-branch review): save_step's own stated contract is
    "nothing typed is lost" on a failed save. The interview answers already
    survived a re-render (test_a_scan_re_renders_with_the_paste_hint above);
    the pasted résumé text itself did not, because the textarea never echoed
    `submitted` -- a paste under 200 characters (too short to extract from)
    used to simply vanish."""
    app = _app(tmp_path, monkeypatch)
    short = "Too short to extract from."
    r = signed_in_client(app).post("/wizard/resume", data={**ANSWERS, "pasted": short})
    assert r.status_code == 200
    assert short in r.text
    assert app.state.service.snapshot().documents.resume_text is None


def test_no_resume_at_all_re_renders(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    r = signed_in_client(app).post("/wizard/resume", data=ANSWERS)
    assert r.status_code == 200
    assert app.state.service.snapshot().documents.resume_text is None


def test_the_form_shows_every_interview_question(tmp_path, monkeypatch):
    from src.resume_intake.interview import INTERVIEW_FIELDS
    r = signed_in_client(_app(tmp_path, monkeypatch)).get("/wizard/resume")
    for f in INTERVIEW_FIELDS:
        assert f'name="{f.name}"' in r.text


def test_interview_multi_questions_are_toggle_groups(tmp_path, monkeypatch):
    html = signed_in_client(_app(tmp_path, monkeypatch)).get("/wizard/resume").text
    assert '<legend class="field-label">What level?</legend>' in html
    assert 'name="seniority" value="staff"><span>Staff</span>' in html
    assert 'value="contract_to_hire"><span>Contract to hire</span>' in html
    assert 'class="checks"' not in html


def test_interview_select_options_read_as_words(tmp_path, monkeypatch):
    html = signed_in_client(_app(tmp_path, monkeypatch)).get("/wizard/resume").text
    assert '<option value="ic" >Individual contributor</option>' in html or \
           '<option value="ic">Individual contributor</option>' in html
    assert ">Only in my countries</option>" in html
