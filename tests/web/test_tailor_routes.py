from fastapi.testclient import TestClient

from src.tailor.endpoint.auth import sign_token
from src.tailor.endpoint.jd import PostingJD
from src.web.app import create_app
from tests.conftest import requires_weasyprint


def _app(tmp_path, monkeypatch):
    monkeypatch.setenv("JOB_AGG_BACKEND", "sqlite")
    monkeypatch.setenv("JOB_AGG_SQLITE_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("JOB_AGG_TAILOR_SIGNING_SECRET", "s3cr3t")
    monkeypatch.setenv("JOB_AGG_TAILORED_DIR", str(tmp_path / "tailored"))
    return create_app()


def test_tailor_rejects_bad_token(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    r = TestClient(app).get("/tailor", params={"job_id": "j1", "t": "bad"})
    assert "expired or is invalid" in r.text


def test_tailor_loading_page_for_valid_token(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    app.state.stores.seen.get_jd = lambda jid: PostingJD("body", "Staff Eng", "Acme")
    import time
    tok = sign_token("j1", int(time.time()) + 600, "s3cr3t")
    r = TestClient(app).get("/tailor", params={"job_id": "j1", "t": tok})
    assert r.status_code == 200
    assert "Staff Eng" in r.text or "Acme" in r.text


def test_loading_page_lists_templates(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    app.state.stores.seen.get_jd = lambda jid: PostingJD("body", "Staff Eng", "Acme")
    import time
    tok = sign_token("j1", int(time.time()) + 600, "s3cr3t")
    r = TestClient(app).get("/tailor", params={"job_id": "j1", "t": tok})
    assert 'id="tpl"' in r.text
    assert "classic" in r.text and "headless" in r.text


@requires_weasyprint
def test_run_with_template_param_rerenders_stored_result(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    app.state.stores.seen.get_jd = lambda jid: PostingJD("body", "T", "C")
    # no engine configured -> tailor_boot None would block run=1; give it a boot
    from src.tailor.content import load_content
    app.state.tailor_boot = (None, load_content("resume/content.example.json"))
    import time
    tok = sign_token("j1", int(time.time()) + 600, "s3cr3t")
    c = TestClient(app)
    first = c.get("/tailor", params={"job_id": "j1", "t": tok, "run": "1"}).json()
    assert "error" not in first
    assert first["template"] == "classic"
    second = c.get("/tailor", params={"job_id": "j1", "t": tok, "run": "1",
                                      "template": "headless"}).json()
    assert "error" not in second
    assert second["template"] == "headless"
    assert second["pdf_url"].endswith("j1.headless.pdf")


@requires_weasyprint
def test_run_falls_back_when_active_template_missing_warns(tmp_path, monkeypatch):
    """The builder settings' active_template can point at a slug that no
    longer exists (deleted pack, etc.). get_template() silently falls back to
    classic; the run output must carry a user-visible warning about it."""
    app = _app(tmp_path, monkeypatch)
    app.state.stores.seen.get_jd = lambda jid: PostingJD("body", "T", "C")
    app.state.stores.builder.put({"active_template": "ghost"})
    from src.tailor.content import load_content
    app.state.tailor_boot = (None, load_content("resume/content.example.json"))
    import time
    tok = sign_token("j1", int(time.time()) + 600, "s3cr3t")
    out = TestClient(app).get(
        "/tailor", params={"job_id": "j1", "t": tok, "run": "1"}).json()
    assert "error" not in out
    assert out["template"] == "classic"
    assert "ghost" in out["fit_warning"]


@requires_weasyprint
def test_run_with_bogus_template_param_falls_back_to_classic(tmp_path, monkeypatch):
    """A junk/bogus `template` query param must not leak into the re-render
    storage key or the echoed template — get_template() resolves it to
    classic and the resolved slug is what's used everywhere (note
    _require_safe_slug does not apply to this route's param; get_template
    just falls back)."""
    app = _app(tmp_path, monkeypatch)
    app.state.stores.seen.get_jd = lambda jid: PostingJD("body", "T", "C")
    from src.tailor.content import load_content
    app.state.tailor_boot = (None, load_content("resume/content.example.json"))
    import time
    tok = sign_token("j1", int(time.time()) + 600, "s3cr3t")
    c = TestClient(app)
    first = c.get("/tailor", params={"job_id": "j1", "t": tok, "run": "1"}).json()
    assert "error" not in first

    r = c.get("/tailor", params={
        "job_id": "j1", "t": tok, "run": "1", "template": "bogus-nonexistent"})
    assert r.status_code == 200
    out = r.json()
    assert "error" not in out
    assert out["template"] == "classic"
    assert out["pdf_url"].endswith("j1.classic.pdf")
