"""The companies step reuses the settings add-by-URL chain rather than
reimplementing the fingerprint/conversion logic.

The probe route's URL, the form field name and the HTMX target id are NOT
assumed here -- src/web/settings/companies.py reads `target` from the posted
form (not `query`), and settings_companies.html's own probe form targets
`#probe-result` (not `#probe-company`). The wizard page below is written to
match the real names."""
import re

import src.web.settings.companies as companies
from src.fingerprint import FingerprintResult
from src.web.app import create_app
from tests.auth_helpers import signed_in_client
from tests.settings_helpers import WEB_TEST_SETTINGS, make_service


def _app(tmp_path, monkeypatch, service=None):
    monkeypatch.setenv("JOB_AGG_SQLITE_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("JOB_AGG_TAILORED_DIR", str(tmp_path / "tailored"))
    return create_app(service=service if service is not None else make_service(WEB_TEST_SETTINGS))


def test_step_offers_the_probe_form(tmp_path, monkeypatch):
    r = signed_in_client(_app(tmp_path, monkeypatch)).get("/wizard/companies")
    assert r.status_code == 200
    assert 'hx-post="/settings/companies/probe"' in r.text
    assert 'name="target"' in r.text


def test_step_links_to_the_full_companies_page(tmp_path, monkeypatch):
    r = signed_in_client(_app(tmp_path, monkeypatch)).get("/wizard/companies")
    assert 'href="/settings/companies"' in r.text


def test_step_shows_already_configured_boards(tmp_path, monkeypatch):
    service = make_service({**WEB_TEST_SETTINGS, "sources": {"greenhouse": ["stripe"]}})
    r = signed_in_client(_app(tmp_path, monkeypatch, service)).get("/wizard/companies")
    assert "stripe" in r.text
    assert "greenhouse" in r.text


def test_step_completes_once_a_board_exists(tmp_path, monkeypatch):
    service = make_service({**WEB_TEST_SETTINGS, "sources": {"greenhouse": ["stripe"]}})
    app = _app(tmp_path, monkeypatch, service)
    client = signed_in_client(app)
    for slug in ("llm", "resume", "review"):
        client.post(f"/wizard/{slug}/skip")
    assert client.get("/wizard", follow_redirects=False).headers["location"] == "/wizard/notifications"


# --- Task 5: the starter pack and discovery checkboxes. Continue is now
# always the submit button of the companies-choice form (never a bare
# `href="/wizard"` link, never a raw `/wizard/companies/skip` post) --
# POST /wizard/companies folds save-or-skip into one route, so a user who
# leaves both boxes unticked and clicks Continue still advances instead of
# looping back to this same page. The two old tests that pinned the
# link-vs-skip-form split are replaced by the checkbox/route tests below,
# which is also why test_continue_button_actually_advances_past_an_empty_
# companies_step (formerly here) is gone too: it posted straight at
# /wizard/companies/skip to prove Continue "really" advanced things, which
# is now exactly what test_submitting_unticked_with_no_boards_skips_and_
# advances below proves against the real route, plus the flags it leaves
# off and the no-re-tick-on-return behaviour. ---

def _checkbox(html, name):
    m = re.search(rf'<input[^>]*name="{name}"[^>]*>', html)
    assert m, f"no {name} checkbox"
    return m.group(0)


def test_both_boxes_pretick_on_first_visit(tmp_path, monkeypatch):
    r = signed_in_client(_app(tmp_path, monkeypatch)).get("/wizard/companies")
    assert "checked" in _checkbox(r.text, "starter_pack")
    assert "checked" in _checkbox(r.text, "discovery")
    assert 'action="/wizard/companies"' in r.text


def test_pack_count_is_shown(tmp_path, monkeypatch):
    from src.starter_pack import PackSlug, StarterPack
    pack = StarterPack("t", tuple(PackSlug("lever", f"c{i}", None, "us", 1) for i in range(7)), ())
    monkeypatch.setattr("src.web.wizard.routes.default_pack", lambda: pack)
    r = signed_in_client(_app(tmp_path, monkeypatch)).get("/wizard/companies")
    assert "7 verified" in r.text


def test_submitting_ticked_turns_both_on_and_advances(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    client = signed_in_client(app)
    for slug in ("llm", "resume", "review"):
        client.post(f"/wizard/{slug}/skip")
    r = client.post("/wizard/companies", data={"starter_pack": "1", "discovery": "1"},
                    follow_redirects=False)
    assert r.status_code == 303
    cfg = app.state.service.snapshot().cfg
    assert cfg.discovery.starter_pack and cfg.discovery.enabled
    assert client.get("/wizard", follow_redirects=False).headers["location"] == "/wizard/notifications"


def test_submitting_unticked_with_no_boards_skips_and_advances(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    client = signed_in_client(app)
    for slug in ("llm", "resume", "review"):
        client.post(f"/wizard/{slug}/skip")
    client.post("/wizard/companies", data={})
    cfg = app.state.service.snapshot().cfg
    assert not cfg.discovery.starter_pack and not cfg.discovery.enabled
    assert "companies" in app.state.stores.wizard.skipped()
    assert client.get("/wizard", follow_redirects=False).headers["location"] == "/wizard/notifications"
    # the user's choice now shows — no re-ticking on return
    page = client.get("/wizard/companies").text
    assert "checked" not in _checkbox(page, "starter_pack")


def test_resubmitting_unchanged_writes_no_new_version(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    client = signed_in_client(app)
    client.post("/wizard/companies", data={"starter_pack": "1", "discovery": "1"})
    before = app.state.service.snapshot().version_id
    r = client.post("/wizard/companies", data={"starter_pack": "1", "discovery": "1"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert app.state.service.snapshot().version_id == before


# --- Mutation-catching: a wrong hx-post target or wrong form field name is
# invisible to the assertions above (a substring match on "/settings/companies"
# or "target" passes even if the actual <form> attribute is spelled
# differently, or if hx-target points at a div id that does not exist -- the
# button then does nothing in a real browser, silently). The tests below
# extract the real attributes from the rendered page and drive the
# interaction end-to-end, so a mis-wired route or field name fails them for
# the right reason instead of passing by coincidence. ---

MATCH = FingerprintResult(
    name="Acme", domain="acme.com", status="matched", family="greenhouse",
    identity={"slug": "acme"}, posting_count=12,
    evidence_url="https://boards.greenhouse.io/acme",
)


def _stub_probe(monkeypatch, result):
    async def fake(client, value, *, name=None):
        return result

    monkeypatch.setattr(companies, "probe_target", fake)


def test_probe_form_actually_reaches_a_working_probe(tmp_path, monkeypatch):
    """Extract the form's own hx-post URL and its query field's own name
    attribute from the rendered page -- not hardcoded assumptions -- then
    replay that exact request against the app. If hx-post named a route that
    doesn't exist, this POST 404s. If the input's name didn't match what
    /settings/companies/probe reads (`target`), the route would see an empty
    value, reject it via normalize_target, and the matched-board content
    below would never show up."""
    _stub_probe(monkeypatch, MATCH)
    app = _app(tmp_path, monkeypatch)
    client = signed_in_client(app)
    page = client.get("/wizard/companies").text

    form_match = re.search(r'<form\b[^>]*\bhx-post="([^"]+)"[^>]*>(.*?)</form>', page, re.S)
    assert form_match, "no hx-post form found on the companies step"
    action, body = form_match.groups()
    field_match = re.search(r'<input\b[^>]*\bname="([^"]+)"', body)
    assert field_match, "no named input inside the probe form"
    field_name = field_match.group(1)

    r = client.post(action, data={field_name: "acme.com"})
    assert r.status_code == 200
    assert "greenhouse" in r.text and "12" in r.text


def test_probe_form_hx_target_points_at_a_real_element(tmp_path, monkeypatch):
    """hx-target picks WHERE the probe result is swapped in. A target id
    with no matching element on the page is a silent no-op in the browser --
    the response comes back fine, htmx just has nowhere to put it. Assert
    the id the form declares as its target actually exists as an element id
    somewhere on the page."""
    r = signed_in_client(_app(tmp_path, monkeypatch)).get("/wizard/companies")
    target_match = re.search(r'<form\b[^>]*\bhx-target="#([\w-]+)"', r.text)
    assert target_match, "no hx-target found on the probe form"
    target_id = target_match.group(1)
    assert f'id="{target_id}"' in r.text
