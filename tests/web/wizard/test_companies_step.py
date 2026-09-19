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


# --- Important 1 (whole-branch review): Continue must not loop. Before any
# board is configured and discovery is off, this step is still incomplete,
# so a plain `href="/wizard"` link would just route right back here
# (next_step() re-picks the first incomplete, unskipped step) — a user who
# adds nothing and clicks Continue would silently land back on the same
# page. Continue must instead post a skip. ---

def test_continue_posts_a_skip_when_no_board_is_configured(tmp_path, monkeypatch):
    r = signed_in_client(_app(tmp_path, monkeypatch)).get("/wizard/companies")
    assert "Continue</button>" in r.text
    assert 'href="/wizard">Continue' not in r.text


def test_continue_links_straight_to_wizard_once_a_board_exists(tmp_path, monkeypatch):
    service = make_service({**WEB_TEST_SETTINGS, "sources": {"greenhouse": ["stripe"]}})
    r = signed_in_client(_app(tmp_path, monkeypatch, service)).get("/wizard/companies")
    assert 'href="/wizard">Continue' in r.text
    assert "Continue</button>" not in r.text


def test_continue_button_actually_advances_past_an_empty_companies_step(tmp_path, monkeypatch):
    """Not just that it LOOKS like a skip form -- posting to it, as a
    browser submitting that form would, must actually move the wizard on."""
    app = _app(tmp_path, monkeypatch)
    client = signed_in_client(app)
    for slug in ("llm", "resume", "review"):
        client.post(f"/wizard/{slug}/skip")
    client.post("/wizard/companies/skip")
    assert client.get("/wizard", follow_redirects=False).headers["location"] == "/wizard/notifications"


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
