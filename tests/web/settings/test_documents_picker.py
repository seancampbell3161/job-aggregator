"""The documents picker shows plain names while the stored kind (and the
?kind= URL) stays the raw value."""
from src.web.app import create_app
from tests.auth_helpers import signed_in_client
from tests.settings_helpers import WEB_TEST_SETTINGS, make_service


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("JOB_AGG_SQLITE_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("JOB_AGG_TAILORED_DIR", str(tmp_path / "tailored"))
    return signed_in_client(create_app(service=make_service(WEB_TEST_SETTINGS)))


def test_documents_picker_shows_plain_names_with_raw_values(tmp_path, monkeypatch):
    html = _client(tmp_path, monkeypatch).get("/settings/documents?kind=kit_facts").text
    assert '<option value="kit_facts" selected>Apply kit facts</option>' in html
    assert '<option value="resume_text" >Résumé (plain text)</option>' in html
