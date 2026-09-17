import time

import pytest
from fastapi.testclient import TestClient

from src.tailor.endpoint.auth import sign_token
from src.tailor.endpoint.jd import PostingJD
from src.web.app import create_app
from src.web.tailor import download_name, pdf_url
from tests.conftest import requires_weasyprint
from tests.settings_helpers import seed_settings


def _app(tmp_path, monkeypatch):
    monkeypatch.setenv("JOB_AGG_SQLITE_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("JOB_AGG_TAILOR_SIGNING_SECRET", "s3cr3t")
    monkeypatch.setenv("JOB_AGG_TAILORED_DIR", str(tmp_path / "tailored"))
    seed_settings({})
    return create_app()


def _token(job_id="j1", secret="s3cr3t", ttl=600):
    return sign_token(job_id, int(time.time()) + ttl, secret)


def _store_pdf(tmp_path, name, data=b"%PDF-1.4 test"):
    d = tmp_path / "tailored"
    d.mkdir(exist_ok=True)
    (d / name).write_bytes(data)


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
    app.state.tailor_boot_override = (None, load_content("resume/content.example.json"))
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
    assert first["pdf_url"] == pdf_url("j1", tok)
    assert second["pdf_url"] == pdf_url("j1", tok, "headless")
    download = c.get(second["pdf_url"])
    assert download.status_code == 200
    assert download.headers["content-type"] == "application/pdf"


@requires_weasyprint
def test_run_falls_back_when_active_template_missing_warns(tmp_path, monkeypatch):
    """The builder settings' active_template can point at a slug that no
    longer exists (deleted pack, etc.). get_template() silently falls back to
    classic; the run output must carry a user-visible warning about it."""
    app = _app(tmp_path, monkeypatch)
    app.state.stores.seen.get_jd = lambda jid: PostingJD("body", "T", "C")
    app.state.stores.builder.put({"active_template": "ghost"})
    from src.tailor.content import load_content
    app.state.tailor_boot_override = (None, load_content("resume/content.example.json"))
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
    app.state.tailor_boot_override = (None, load_content("resume/content.example.json"))
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
    assert out["pdf_url"] == pdf_url("j1", tok, "classic")


def test_tailor_rejects_links_before_setup(tmp_path, monkeypatch):
    monkeypatch.setenv("JOB_AGG_SQLITE_PATH", str(tmp_path / "never-configured.db"))
    monkeypatch.setenv("JOB_AGG_TAILOR_SIGNING_SECRET", "s3cr3t")
    monkeypatch.setenv("JOB_AGG_TAILORED_DIR", str(tmp_path / "tailored"))
    import time
    tok = sign_token("j1", int(time.time()) + 600, "s3cr3t")
    r = TestClient(create_app()).get("/tailor", params={"job_id": "j1", "t": tok})
    assert r.status_code == 200
    assert "expired or is invalid" in r.text


def test_tailor_verifies_with_the_stored_signing_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("JOB_AGG_SQLITE_PATH", str(tmp_path / "t.db"))
    monkeypatch.delenv("JOB_AGG_TAILOR_SIGNING_SECRET", raising=False)
    monkeypatch.setenv("JOB_AGG_TAILORED_DIR", str(tmp_path / "tailored"))
    seed_settings({}, secrets={"tailor_signing_secret": "from-db"})
    app = create_app()
    app.state.stores.seen.get_jd = lambda jid: PostingJD("body", "Staff Eng", "Acme")
    import time
    tok = sign_token("j1", int(time.time()) + 600, "from-db")
    r = TestClient(app).get("/tailor", params={"job_id": "j1", "t": tok})
    assert "Staff Eng" in r.text or "Acme" in r.text


def test_run_reports_tailoring_disabled_from_settings(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)  # tailoring.enabled defaults to false
    import time
    tok = sign_token("j1", int(time.time()) + 600, "s3cr3t")
    out = TestClient(app).get("/tailor", params={"job_id": "j1", "t": tok, "run": "1"}).json()
    assert out == {"error": "Tailoring is not enabled on this server."}


def test_run_survives_a_failing_tailor_engine_builder(tmp_path, monkeypatch):
    """Ruling G: build_tailor_engine raising must not 500 the run — the
    engine degrades to None (a stored result can still be re-rendered), so
    with tailoring enabled and a resume_content document saved, run=1 must
    come back as JSON, not a 500 and not the 'not enabled' error."""
    from pathlib import Path

    import src.tailor as tailor_mod

    monkeypatch.setenv("JOB_AGG_SQLITE_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("JOB_AGG_TAILOR_SIGNING_SECRET", "s3cr3t")
    monkeypatch.setenv("JOB_AGG_TAILORED_DIR", str(tmp_path / "tailored"))
    content = Path("resume/content.example.json").read_text()
    seed_settings({"tailoring": {"enabled": True}}, documents={"resume_content": content})

    def boom(cfg, content, evidence):
        raise RuntimeError("boom")

    monkeypatch.setattr(tailor_mod, "build_tailor_engine", boom)
    app = create_app()
    app.state.stores.seen.get_jd = lambda jid: PostingJD("body", "T", "C")
    import time
    tok = sign_token("j1", int(time.time()) + 600, "s3cr3t")
    r = TestClient(app).get("/tailor", params={"job_id": "j1", "t": tok, "run": "1"})
    assert r.status_code == 200
    body = r.json()
    assert body != {"error": "Tailoring is not enabled on this server."}


def test_pdf_url_and_download_name():
    assert pdf_url("greenhouse:acme:1", "123.a-b_c") == (
        "/tailor/pdf?job_id=greenhouse%3Aacme%3A1&t=123.a-b_c")
    assert pdf_url("j1", "tok", "headless") == "/tailor/pdf?job_id=j1&t=tok&template=headless"
    assert download_name("greenhouse:acme:1") == "resume-greenhouse-acme-1.pdf"


def test_run_returns_a_token_checked_pdf_url(tmp_path, monkeypatch):
    import src.web.tailor as tailor_routes
    app = _app(tmp_path, monkeypatch)
    app.state.tailor_boot_override = (None, object())
    monkeypatch.setattr(tailor_routes, "run_tailor",
                        lambda **kw: {"pdf_key": "j1.pdf", "cached": True})
    tok = _token()
    out = TestClient(app).get("/tailor", params={"job_id": "j1", "t": tok, "run": "1"}).json()
    assert out == {"pdf_url": pdf_url("j1", tok), "cached": True}


def test_pdf_route_serves_a_stored_pdf_with_the_deep_link_token(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    _store_pdf(tmp_path, "greenhouse:acme:1.pdf")
    r = TestClient(app).get("/tailor/pdf", params={
        "job_id": "greenhouse:acme:1", "t": _token("greenhouse:acme:1")})
    assert r.status_code == 200
    assert r.content == b"%PDF-1.4 test"
    assert r.headers["content-type"] == "application/pdf"
    assert r.headers["content-disposition"] == 'inline; filename="resume-greenhouse-acme-1.pdf"'
    assert r.headers["cache-control"] == "private, no-store"
    assert r.headers["x-content-type-options"] == "nosniff"


def test_pdf_route_serves_a_template_rerender(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    _store_pdf(tmp_path, "j1.headless.pdf", b"%PDF-headless")
    r = TestClient(app).get("/tailor/pdf", params={
        "job_id": "j1", "t": _token(), "template": "headless"})
    assert r.status_code == 200 and r.content == b"%PDF-headless"


@pytest.mark.parametrize("token", ["", "bad", "123.sig"])
def test_pdf_route_rejects_bad_tokens(tmp_path, monkeypatch, token):
    app = _app(tmp_path, monkeypatch)
    _store_pdf(tmp_path, "j1.pdf")
    r = TestClient(app).get("/tailor/pdf", params={"job_id": "j1", "t": token})
    assert r.status_code == 403 and "expired or is invalid" in r.text


def test_pdf_route_rejects_an_expired_token(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    _store_pdf(tmp_path, "j1.pdf")
    r = TestClient(app).get("/tailor/pdf", params={"job_id": "j1", "t": _token(ttl=-1)})
    assert r.status_code == 403


def test_a_token_for_one_job_cannot_fetch_another(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    _store_pdf(tmp_path, "j2.pdf")
    r = TestClient(app).get("/tailor/pdf", params={"job_id": "j2", "t": _token("j1")})
    assert r.status_code == 403


@pytest.mark.parametrize("template", ["../x", "Headless", ".hidden", "a/b", "-x"])
def test_pdf_route_rejects_crafted_template_slugs(tmp_path, monkeypatch, template):
    app = _app(tmp_path, monkeypatch)
    r = TestClient(app).get("/tailor/pdf", params={
        "job_id": "j1", "t": _token(), "template": template})
    assert r.status_code == 400


def test_pdf_route_never_serves_the_stored_render_input(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    _store_pdf(tmp_path, "j1.result.json", b"{}")
    r = TestClient(app).get("/tailor/pdf", params={
        "job_id": "j1", "t": _token(), "template": "result"})
    assert r.status_code == 404


def test_pdf_route_404s_a_missing_pdf(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    r = TestClient(app).get("/tailor/pdf", params={"job_id": "j1", "t": _token()})
    assert r.status_code == 404 and "isn't stored anymore" in r.text


def test_pdf_route_stays_inside_the_tailored_dir(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    (tmp_path / "tailored").mkdir()
    (tmp_path / "escape.pdf").write_bytes(b"%PDF-outside")
    r = TestClient(app).get("/tailor/pdf", params={
        "job_id": "../escape", "t": _token("../escape")})
    assert r.status_code == 404


def test_pdf_route_rejects_links_before_setup(tmp_path, monkeypatch):
    monkeypatch.setenv("JOB_AGG_SQLITE_PATH", str(tmp_path / "never-configured.db"))
    monkeypatch.setenv("JOB_AGG_TAILOR_SIGNING_SECRET", "s3cr3t")
    monkeypatch.setenv("JOB_AGG_TAILORED_DIR", str(tmp_path / "tailored"))
    _store_pdf(tmp_path, "j1.pdf")
    r = TestClient(create_app()).get("/tailor/pdf", params={"job_id": "j1", "t": _token()})
    assert r.status_code == 403


def test_the_tailored_static_mount_is_gone(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    _store_pdf(tmp_path, "j1.pdf")
    assert TestClient(app).get("/tailored/j1.pdf").status_code == 404
