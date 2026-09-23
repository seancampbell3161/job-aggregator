"""Entry, navigation and skip. The wizard writes a settings version at entry,
so every /wizard/* page is an ordinary post-setup page — no new setup-exempt
prefix and no middleware change."""
import pytest

from src.web.app import create_app
from tests.auth_helpers import signed_in_client
from tests.settings_helpers import WEB_TEST_SETTINGS, make_service


def _app(tmp_path, monkeypatch, service=None):
    monkeypatch.setenv("JOB_AGG_SQLITE_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("JOB_AGG_TAILORED_DIR", str(tmp_path / "tailored"))
    return create_app(service=service if service is not None else make_service(WEB_TEST_SETTINGS))


def _unconfigured(tmp_path, monkeypatch):
    return _app(tmp_path, monkeypatch, service=make_service(None))


def test_setup_offers_guided_setup(tmp_path, monkeypatch):
    r = signed_in_client(_unconfigured(tmp_path, monkeypatch)).get("/setup")
    assert r.status_code == 200
    assert 'action="/setup/wizard"' in r.text


def test_wizard_entry_writes_a_version_and_redirects(tmp_path, monkeypatch):
    app = _unconfigured(tmp_path, monkeypatch)
    client = signed_in_client(app)
    r = client.post("/setup/wizard", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/wizard"
    snap = app.state.service.snapshot()
    assert snap is not None
    assert app.state.service.versions(1)[0].source == "wizard"


def test_wizard_entry_is_idempotent(tmp_path, monkeypatch):
    """Two visitors racing the button must not write two versions."""
    app = _unconfigured(tmp_path, monkeypatch)
    client = signed_in_client(app)
    client.post("/setup/wizard")
    client.post("/setup/wizard")
    assert len(app.state.service.versions(None)) == 1


def test_wizard_root_redirects_to_the_next_step(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    r = signed_in_client(app).get("/wizard", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/wizard/llm"


def test_a_step_page_renders(tmp_path, monkeypatch):
    r = signed_in_client(_app(tmp_path, monkeypatch)).get("/wizard/llm")
    assert r.status_code == 200
    assert "Connect an LLM" in r.text


def test_the_progress_rail_lists_every_step(tmp_path, monkeypatch):
    r = signed_in_client(_app(tmp_path, monkeypatch)).get("/wizard/llm")
    for title in ("Connect an LLM", "Your résumé", "What would match"):
        assert title in r.text


def test_exactly_one_step_is_announced_as_current(tmp_path, monkeypatch):
    """A screen-reader user tabbing through the rail needs to hear which step
    they're on; aria-current="step" is how that's announced."""
    r = signed_in_client(_app(tmp_path, monkeypatch)).get("/wizard/llm")
    assert r.text.count('aria-current="step"') == 1


def test_unknown_step_is_404(tmp_path, monkeypatch):
    assert signed_in_client(_app(tmp_path, monkeypatch)).get("/wizard/nope").status_code == 404


def test_skip_records_and_advances(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    client = signed_in_client(app)
    r = client.post("/wizard/llm/skip", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/wizard"
    assert app.state.stores.wizard.skipped() == {"llm"}
    assert client.get("/wizard", follow_redirects=False).headers["location"] == "/wizard/resume"


def test_skipping_everything_lands_on_done(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    client = signed_in_client(app)
    for slug in ("llm", "resume", "review", "companies", "notifications", "preview"):
        client.post(f"/wizard/{slug}/skip")
    r = client.get("/wizard", follow_redirects=False)
    assert r.headers["location"] == "/wizard/done"


def test_done_page_renders(tmp_path, monkeypatch):
    r = signed_in_client(_app(tmp_path, monkeypatch)).get("/wizard/done")
    assert r.status_code == 200
    assert "/settings/overview" in r.text


def test_the_step_body_is_wrapped_for_width_and_the_rail_stays_outside_it(tmp_path, monkeypatch):
    """.field controls are width: 100% since Task 1, so on a wide screen an
    unwrapped step stretches its cards and inputs across the whole window.
    wizard.css caps .wizard-body the same way settings.css caps
    .settings-pane. The rail must stay outside that wrapper — it is not part
    of the step content whose width is being capped."""
    html = signed_in_client(_app(tmp_path, monkeypatch)).get("/wizard/llm").text
    assert 'class="wizard-body"' in html
    rail_start = html.index('class="wizard-rail"')
    rail_end = html.index("</nav>", rail_start)
    body_start = html.index('class="wizard-body"')
    assert rail_end < body_start, "wizard-rail nav must appear before wizard-body"
    body_div_open = html.rindex("<div", 0, body_start)
    assert "wizard-rail" not in html[body_div_open:], (
        "wizard-rail nav must not be nested inside the wizard-body wrapper"
    )


def test_wizard_requires_a_login(tmp_path, monkeypatch):
    """Brief's original version hit /wizard/llm with no claimed password, so
    the login gate would send it to /welcome (no password set yet) rather
    than /login, and the startswith("/login") assertion would fail for the
    wrong reason. Claim a password first, same as
    test_settings_needs_a_session in tests/web/settings/test_shell.py, so this
    actually exercises the session gate the test is named for."""
    from fastapi.testclient import TestClient

    app = _app(tmp_path, monkeypatch)
    app.state.auth.set_password("correct horse")
    r = TestClient(app, follow_redirects=False).get("/wizard/llm")
    assert r.status_code == 303
    assert r.headers["location"].startswith("/login")


def test_every_step_has_a_template():
    """A WIZARD_STEPS entry with no TEMPLATES row 500s on render — and the
    step it strands is unreachable, because next_step still returns it.

    Asserts the actual slug -> filename pairing, not just that the two sets
    of keys match: comparing only `set(TEMPLATES)` against the step slugs
    would not catch two slugs having their template values swapped (e.g.
    "llm": "wizard_resume.html", "resume": "wizard_llm.html" passes a
    set-equality check but renders the wrong page for both steps)."""
    from src.web.wizard.routes import TEMPLATES
    from src.web.wizard.steps import WIZARD_STEPS
    assert set(TEMPLATES) == {s.slug for s in WIZARD_STEPS}
    for slug in TEMPLATES:
        assert TEMPLATES[slug] == f"wizard_{slug}.html"


def test_wizard_is_not_setup_exempt(tmp_path, monkeypatch):
    """The entry POST writes the version, so /wizard itself never needs to be
    reachable before setup — and must not be."""
    from src.web.app import _setup_exempt
    assert _setup_exempt("/wizard") is False
    assert _setup_exempt("/wizard/llm") is False


def test_the_done_page_offers_tailoring_as_an_optional_next_step(tmp_path, monkeypatch):
    """The wizard stays six steps; tailoring is offered after it, not inside
    it — it needs a cloud LLM and the render extra, which a newcomer may not
    have on day one."""
    r = signed_in_client(_app(tmp_path, monkeypatch)).get("/wizard/done")
    assert "/settings/documents/draft" in r.text
    assert "tailored résumés" in r.text.lower()
