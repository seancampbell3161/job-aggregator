"""Profile and the generic document editor."""
from src.web.app import create_app
from tests.auth_helpers import signed_in_client
from tests.settings_helpers import WEB_TEST_SETTINGS, make_service


def _app(tmp_path, monkeypatch, service=None):
    monkeypatch.setenv("JOB_AGG_SQLITE_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("JOB_AGG_TAILORED_DIR", str(tmp_path / "tailored"))
    return create_app(service=service if service is not None else make_service(WEB_TEST_SETTINGS))


def test_profile_page_shows_the_stored_profile(tmp_path, monkeypatch):
    service = make_service(WEB_TEST_SETTINGS, documents={"profile": "# Me\nStaff engineer."})
    r = signed_in_client(_app(tmp_path, monkeypatch, service)).get("/settings/profile")
    assert r.status_code == 200
    assert "Staff engineer." in r.text


def test_profile_save_round_trips(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    r = signed_in_client(app).post("/settings/profile", data={"body": "# New profile"})
    assert r.status_code == 200
    assert app.state.service.snapshot().documents.profile == "# New profile"


def test_empty_profile_is_rejected(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    r = signed_in_client(app).post("/settings/profile", data={"body": "   "})
    assert r.status_code == 200
    assert "must not be empty" in r.text
    assert app.state.service.snapshot().documents.profile is None


def test_documents_page_defaults_to_the_first_kind(tmp_path, monkeypatch):
    r = signed_in_client(_app(tmp_path, monkeypatch)).get("/settings/documents")
    assert r.status_code == 200
    assert 'name="kind"' in r.text
    assert "resume_text" in r.text


def test_documents_page_loads_a_requested_kind(tmp_path, monkeypatch):
    service = make_service(WEB_TEST_SETTINGS, documents={"resume_text": "# CV"})
    r = signed_in_client(_app(tmp_path, monkeypatch, service)).get(
        "/settings/documents", params={"kind": "resume_text"})
    assert "# CV" in r.text


def test_saving_a_structured_document_validates_it(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    r = signed_in_client(app).post(
        "/settings/documents", data={"kind": "kit_facts", "body": "not: [valid"})
    assert r.status_code == 200
    assert "kit_facts" in r.text
    assert app.state.service.snapshot().documents.kit_facts is None


def test_unknown_kind_is_rejected(tmp_path, monkeypatch):
    r = signed_in_client(_app(tmp_path, monkeypatch)).post(
        "/settings/documents", data={"kind": "nope", "body": "x"})
    assert r.status_code == 400


def test_profile_is_not_offered_in_the_generic_editor(tmp_path, monkeypatch):
    r = signed_in_client(_app(tmp_path, monkeypatch)).get("/settings/documents")
    assert '<option value="profile"' not in r.text


def test_document_body_is_escaped_in_the_textarea(tmp_path, monkeypatch):
    """A document body is user-supplied text rendered into a <textarea>; it
    must not be able to break out of it, even before validation runs."""
    payload = "</textarea><script>alert(1)</script>"
    service = make_service(WEB_TEST_SETTINGS, documents={"resume_text": payload})
    r = signed_in_client(_app(tmp_path, monkeypatch, service)).get(
        "/settings/documents", params={"kind": "resume_text"})
    assert r.status_code == 200
    assert "<script>alert(1)</script>" not in r.text
    assert "</textarea><script>" not in r.text


def test_the_documents_page_links_to_drafting(tmp_path, monkeypatch):
    r = signed_in_client(_app(tmp_path, monkeypatch)).get("/settings/documents")
    assert "/settings/documents/draft" in r.text
