"""The integrations section: off-ATS discovery keys, Gmail ingestion, and the
tailoring deep-link endpoint — secrets with no settings paths to hang off, so
this section has no fields and no Test button, only the write-only secret rows."""
from src.web.app import create_app
from tests.auth_helpers import signed_in_client
from tests.settings_helpers import WEB_TEST_SETTINGS, make_service


def _app(tmp_path, monkeypatch, service=None):
    monkeypatch.setenv("JOB_AGG_SQLITE_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("JOB_AGG_TAILORED_DIR", str(tmp_path / "tailored"))
    return create_app(service=service if service is not None else make_service(WEB_TEST_SETTINGS))


def test_integrations_section_follows_notifications():
    from src.web.settings.sections import SECTIONS
    slugs = [s.slug for s in SECTIONS]
    i = slugs.index("notifications")
    assert slugs[i + 1] == "integrations"


def test_page_renders_every_claimed_secret(tmp_path, monkeypatch):
    r = signed_in_client(_app(tmp_path, monkeypatch)).get("/settings/integrations")
    assert r.status_code == 200
    for name in ("adzuna_app_id", "adzuna_app_key", "gmail_address",
                 "gmail_app_password", "tailor_endpoint_url"):
        assert name in r.text
    # no Test button on a secrets-only page
    assert "hx-post" not in r.text


def test_a_secret_saves(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    r = signed_in_client(app).post(
        "/settings/integrations", data={"secret.adzuna_app_id": "abc123"})
    assert r.status_code == 200
    assert app.state.service.secret_source("adzuna_app_id") == "stored"


def test_a_stored_value_never_appears_in_the_response_body(tmp_path, monkeypatch):
    service = make_service(WEB_TEST_SETTINGS, secrets={"gmail_app_password": "super-secret-value"})
    app = _app(tmp_path, monkeypatch, service)
    r = signed_in_client(app).get("/settings/integrations")
    assert "super-secret-value" not in r.text
    assert "Saved" in r.text


def test_a_stored_value_never_appears_after_a_failed_save(tmp_path, monkeypatch):
    """decode() for a paths-less section always succeeds, so there is no
    invalid-settings failure mode here — but the page can still be re-rendered
    (e.g. after a plain save), and a stored secret must never leak into it."""
    service = make_service(WEB_TEST_SETTINGS, secrets={"gmail_app_password": "super-secret-value"})
    app = _app(tmp_path, monkeypatch, service)
    r = signed_in_client(app).post("/settings/integrations", data={})
    assert "super-secret-value" not in r.text


def test_tailor_signing_secret_stays_unclaimed_and_cli_only(tmp_path, monkeypatch):
    from src.web.settings.sections import UNCLAIMED_SECRETS
    assert UNCLAIMED_SECRETS == {"tailor_signing_secret"}
