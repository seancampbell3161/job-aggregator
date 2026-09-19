"""Add a company: probe, then confirm."""
import src.web.settings.companies as companies
from src.fingerprint import FingerprintResult
from src.web.app import create_app
from tests.auth_helpers import signed_in_client
from tests.settings_helpers import make_service


def _app(tmp_path, monkeypatch, service=None):
    monkeypatch.setenv("JOB_AGG_SQLITE_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("JOB_AGG_TAILORED_DIR", str(tmp_path / "tailored"))
    return create_app(service=service if service is not None else make_service({}))


def _stub(monkeypatch, result):
    async def fake(client, value, *, name=None):
        return result

    monkeypatch.setattr(companies, "probe_target", fake)


MATCH = FingerprintResult(
    name="Acme", domain="acme.com", status="matched", family="greenhouse",
    identity={"slug": "acme"}, posting_count=12,
    evidence_url="https://boards.greenhouse.io/acme",
)


def test_a_match_shows_the_entry_and_an_add_button(tmp_path, monkeypatch):
    _stub(monkeypatch, MATCH)
    app = _app(tmp_path, monkeypatch)
    r = signed_in_client(app).post("/settings/companies/probe", data={"target": "acme.com"})
    assert r.status_code == 200
    assert "greenhouse" in r.text and "12" in r.text
    assert "/settings/companies/add" in r.text
    assert app.state.service.snapshot().cfg.sources.greenhouse == []  # a probe writes nothing


def test_an_already_configured_board_offers_no_add(tmp_path, monkeypatch):
    _stub(monkeypatch, MATCH)
    app = _app(tmp_path, monkeypatch, make_service({"sources": {"greenhouse": ["acme"]}}))
    r = signed_in_client(app).post("/settings/companies/probe", data={"target": "acme.com"})
    assert "already" in r.text.lower()
    assert "/settings/companies/add" not in r.text


def test_an_empty_board_is_addable_but_says_it_is_empty(tmp_path, monkeypatch):
    _stub(monkeypatch, FingerprintResult(
        name="Acme", domain="acme.com", status="not_found", family="lever",
        identity={"slug": "acme"}, note="fingerprinted but board verified empty"))
    r = signed_in_client(_app(tmp_path, monkeypatch)).post(
        "/settings/companies/probe", data={"target": "acme.com"})
    assert "verified empty" in r.text
    assert "/settings/companies/add" in r.text


def test_an_unsupported_ats_points_at_the_manual_form(tmp_path, monkeypatch):
    """Whole-branch review: the old assertion (`"/settings/rows/" in r.text`)
    would still pass even if the href pointed at the wrong family, or even
    if it were missing entirely and the substring only came from the
    Companies page's own "add a board manually" family picker (Important
    3) — that picker links `/settings/rows/sources.<family>/new` for
    EVERY board family unconditionally, on every render. Pinning the exact
    href for THIS family is what actually proves the unsupported card's own
    link is correct."""
    _stub(monkeypatch, FingerprintResult(
        name="Acme", domain="acme.com", status="unsupported", family="bamboohr",
        evidence_url="https://acme.bamboohr.com"))
    r = signed_in_client(_app(tmp_path, monkeypatch)).post(
        "/settings/companies/probe", data={"target": "acme.com"})
    assert "bamboohr" in r.text
    assert 'href="/settings/rows/sources.bamboohr/new"' in r.text


def test_an_ambiguous_jsonld_probe_points_the_manual_form_at_jsonld_boards(tmp_path, monkeypatch):
    """Minor 1 (whole-branch review): fingerprint_company's ambiguous
    branch (multiple ATS families seen, none of them a clean single match)
    can set family to the literal string "jsonld" itself — not just
    icims/successfactors/talentbrew — when the first candidate it saw was a
    jsonld match (src/fingerprint.py:468-476, `fam = supported[0][0]`).
    _manual_row_path("jsonld") builds "sources.jsonld", which is not in
    editable_row_paths() (the config key is sources.jsonld_boards) — a
    404ing link. The fix routes it through _board_family first, which
    already does exactly this "jsonld" -> "jsonld_boards" translation."""
    _stub(monkeypatch, FingerprintResult(
        name="Acme", domain="acme.com", status="unsupported", family="jsonld",
        note="multiple ATS fingerprints seen: eightfold, jsonld"))
    client = signed_in_client(_app(tmp_path, monkeypatch))
    r = client.post("/settings/companies/probe", data={"target": "acme.com"})
    assert 'href="/settings/rows/sources.jsonld_boards/new"' in r.text
    assert 'href="/settings/rows/sources.jsonld/new"' not in r.text
    # Prove the link is actually reachable, not just textually different.
    follow = client.get("/settings/rows/sources.jsonld_boards/new")
    assert follow.status_code == 200


def test_a_probe_that_failed_shows_the_error_instead_of_a_blank_card(tmp_path, monkeypatch):
    _stub(monkeypatch, FingerprintResult(
        name="acme.com", domain="acme.com", status="error",
        note="ConnectTimeout: timed out"))
    r = signed_in_client(_app(tmp_path, monkeypatch)).post(
        "/settings/companies/probe", data={"target": "acme.com"})
    assert r.status_code == 200
    assert "ConnectTimeout" in r.text
    assert "/settings/companies/add" not in r.text


def test_a_family_less_not_found_shows_no_nonsense_card(tmp_path, monkeypatch):
    """Important 1 (whole-branch review): fingerprint_company's chain
    locating nothing at all (src/fingerprint.py:478) returns
    FingerprintResult(status="not_found") with family=None, identity=None,
    note=None -- the most common outcome for an arbitrary company domain
    not on a supported ATS. The template's first branch used to match on
    status alone, rendering "Acme · None" / "None" / an Add button posting
    family="None" back to /settings/companies/add, which 400s
    ("unknown source family"). No existing fixture in this file supplies a
    family-less not_found: every not_found here carries both a family and
    an identity."""
    _stub(monkeypatch, FingerprintResult(name="Acme", domain="acme.com", status="not_found"))
    r = signed_in_client(_app(tmp_path, monkeypatch)).post(
        "/settings/companies/probe", data={"target": "acme.com"})
    assert r.status_code == 200
    assert "None" not in r.text
    assert "/settings/companies/add" not in r.text
    assert "couldn" in r.text.lower()


def test_an_error_with_a_known_family_still_offers_the_manual_form(tmp_path, monkeypatch):
    """Important 3 (whole-branch review): the spec's probe-outcome table
    requires the manual form on BOTH "unsupported/ambiguous" and
    "error/timeout" rows; only the unsupported branch rendered it. Today's
    fingerprint_company never actually sets a family on an "error" result,
    but the template must not silently drop the link if some future change
    starts leaving one -- this pins that the else-branch (which handles
    both "error" and a family-less "not_found") renders it whenever
    manual_path is available, not just on the unsupported branch."""
    _stub(monkeypatch, FingerprintResult(
        name="Acme", domain="acme.com", status="error", family="phenom",
        note="ConnectTimeout: timed out"))
    r = signed_in_client(_app(tmp_path, monkeypatch)).post(
        "/settings/companies/probe", data={"target": "acme.com"})
    assert r.status_code == 200
    assert "ConnectTimeout" in r.text
    assert 'href="/settings/rows/sources.phenom/new"' in r.text


def test_an_unsupported_result_does_not_crash_on_a_missing_identity(tmp_path, monkeypatch):
    """connector_name() needs family AND identity; an unsupported result has
    only a family. Calling it unguarded is a 500 on the unhappy path."""
    _stub(monkeypatch, FingerprintResult(
        name="Acme", domain="acme.com", status="unsupported", family="bamboohr"))
    r = signed_in_client(_app(tmp_path, monkeypatch)).post(
        "/settings/companies/probe", data={"target": "acme.com"})
    assert r.status_code == 200


def test_a_blank_target_reports_instead_of_probing(tmp_path, monkeypatch):
    r = signed_in_client(_app(tmp_path, monkeypatch)).post(
        "/settings/companies/probe", data={"target": "   "})
    assert r.status_code == 200
    assert "careers page URL" in r.text


def test_evidence_url_renders_as_a_safe_link(tmp_path, monkeypatch):
    """Step 5's own requirement: the evidence URL is a link with
    rel="noopener noreferrer" — nothing in the brief's own test list actually
    checks this, so a template that forgot the attribute would still pass
    every test the brief wrote."""
    _stub(monkeypatch, MATCH)
    r = signed_in_client(_app(tmp_path, monkeypatch)).post(
        "/settings/companies/probe", data={"target": "acme.com"})
    assert MATCH.evidence_url in r.text
    assert 'rel="noopener noreferrer"' in r.text


def test_the_add_box_posts_the_field_name_the_probe_route_reads(tmp_path, monkeypatch):
    """The probe route reads form field "target" (both the brief's route code
    and its own tests agree on that name). settings_companies.html (Task 7)
    shipped with an add-box input named "url" instead — a silent mismatch: the
    hx-post would always submit an empty target and every real paste would
    dead-end at "enter a careers page URL or a company domain". This is the
    kind of defect the task brief warns every task on this branch has one of."""
    r = signed_in_client(_app(tmp_path, monkeypatch)).get("/settings/companies")
    assert r.status_code == 200
    assert 'name="target"' in r.text
    assert 'name="url"' not in r.text


def test_add_writes_the_slug(tmp_path, monkeypatch):
    """signed_in_client(app) alone follows the redirect (TestClient's own
    default), so a 303 would read back as the followed page's 200 — exactly
    the kind of defect the task brief warns every brief on this branch has
    one of; follow_redirects=False (tests/web/settings/test_rows.py's own
    convention for the same check) is required to observe it."""
    app = _app(tmp_path, monkeypatch)
    r = signed_in_client(app, follow_redirects=False).post("/settings/companies/add", data={
        "family": "greenhouse", "name": "Acme", "identity": '{"slug": "acme"}'})
    assert r.status_code == 303
    assert app.state.service.snapshot().cfg.sources.greenhouse == ["acme"]


def test_add_writes_a_structured_entry_with_its_company_name(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    signed_in_client(app).post("/settings/companies/add", data={
        "family": "taleo", "name": "Cincinnati Financial",
        "identity": '{"tenant": "cinfin", "section": "ex"}'})
    board = app.state.service.snapshot().cfg.sources.taleo[0]
    assert (board.tenant, board.section, board.company) == ("cinfin", "ex", "Cincinnati Financial")


def test_add_refuses_a_tampered_identity(tmp_path, monkeypatch):
    """The hidden fields are user input. A payload the model rejects must not
    reach the settings document."""
    app = _app(tmp_path, monkeypatch)
    r = signed_in_client(app).post("/settings/companies/add", data={
        "family": "eightfold", "name": "x",
        "identity": '{"slug": "ngc", "domain": "ngc.com", "flavor": "evil"}'})
    assert r.status_code == 400
    assert app.state.service.snapshot().cfg.sources.eightfold == []


def test_add_refuses_an_unknown_family(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    r = signed_in_client(app).post("/settings/companies/add", data={
        "family": "../../etc", "name": "x", "identity": "{}"})
    assert r.status_code == 400


def test_add_is_idempotent_for_a_board_already_configured(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, make_service({"sources": {"greenhouse": ["acme"]}}))
    signed_in_client(app).post("/settings/companies/add", data={
        "family": "greenhouse", "name": "Acme", "identity": '{"slug": "acme"}'})
    assert app.state.service.snapshot().cfg.sources.greenhouse == ["acme"]


def test_add_refuses_an_incomplete_structured_identity_without_a_500(tmp_path, monkeypatch):
    """A tampered identity missing required keys (no region/site for a
    workday tenant) must fail cleanly through add_row_patch's own validation.
    If the idempotency check ran first and indexed the raw, unvalidated
    identity dict (board_key() does entry['tenant']/entry['site']), a missing
    key would raise an uncaught KeyError instead of a 400."""
    app = _app(tmp_path, monkeypatch)
    r = signed_in_client(app).post("/settings/companies/add", data={
        "family": "workday", "name": "x", "identity": '{"tenant": "acme"}'})
    assert r.status_code == 400
    assert app.state.service.snapshot().cfg.sources.workday == []


def test_add_refuses_malformed_identity_json(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    r = signed_in_client(app).post("/settings/companies/add", data={
        "family": "greenhouse", "name": "x", "identity": "not json"})
    assert r.status_code == 400


def test_add_refuses_a_non_object_identity(tmp_path, monkeypatch):
    """identity must decode to a JSON object — a list or bare scalar carries
    no field names for add_row_patch to validate against and would otherwise
    blow up deeper in the stack instead of failing cleanly here."""
    app = _app(tmp_path, monkeypatch)
    r = signed_in_client(app).post("/settings/companies/add", data={
        "family": "greenhouse", "name": "x", "identity": "[1, 2, 3]"})
    assert r.status_code == 400


def test_a_jsonld_match_is_addable_under_its_sources_family(tmp_path, monkeypatch):
    """FingerprintResult.family for an iCIMS/SuccessFactors/TalentBrew match is
    the literal string "jsonld" — but the settings field that stores those
    boards is sources.jsonld_boards. Posting the hidden family field back as
    "jsonld" would 400 (not in BOARD_FAMILIES); the probe card must offer the
    sources-config family name instead."""
    _stub(monkeypatch, FingerprintResult(
        name="Acme", domain="acme.com", status="matched", family="jsonld",
        identity={"family": "icims", "slug": "acme", "base_url": "https://careers-acme.icims.com"},
        posting_count=3, evidence_url="https://careers-acme.icims.com",
    ))
    app = _app(tmp_path, monkeypatch)
    r = signed_in_client(app).post("/settings/companies/probe", data={"target": "acme.com"})
    assert 'name="family" value="jsonld_boards"' in r.text
    assert 'name="family" value="jsonld"' not in r.text

    r2 = signed_in_client(app, follow_redirects=False).post("/settings/companies/add", data={
        "family": "jsonld_boards", "name": "Acme",
        "identity": '{"family": "icims", "slug": "acme", "base_url": "https://careers-acme.icims.com"}',
    })
    assert r2.status_code == 303
    board = app.state.service.snapshot().cfg.sources.jsonld_boards[0]
    assert (board.family, board.slug, board.base_url, board.company) == (
        "icims", "acme", "https://careers-acme.icims.com", "Acme",
    )


# --- `next` threading (whole-branch review, Important 1): the wizard's
# companies step reuses this same probe/add chain rather than reimplementing
# it, but companies_add used to ALWAYS redirect to /settings/companies —
# stranding a wizard user on the standalone settings page, which has no
# route back into the wizard. The Check form now carries a `next` field
# through the probe result into "Add this board", and companies_add honours
# it (re-validated with safe_next() at the point of use, since the hidden
# field is client-controlled HTML a tampered POST could resubmit). ---

def test_probe_result_carries_next_into_the_add_form(tmp_path, monkeypatch):
    _stub(monkeypatch, MATCH)
    r = signed_in_client(_app(tmp_path, monkeypatch)).post(
        "/settings/companies/probe", data={"target": "acme.com", "next": "/wizard"})
    assert 'name="next" value="/wizard"' in r.text


def test_probe_result_omits_next_when_none_was_given(tmp_path, monkeypatch):
    """The settings page's own Check form never sends `next` — its add box
    must keep redirecting to this same page, not gain a stray hidden field."""
    _stub(monkeypatch, MATCH)
    r = signed_in_client(_app(tmp_path, monkeypatch)).post(
        "/settings/companies/probe", data={"target": "acme.com"})
    assert 'name="next"' not in r.text


def test_add_with_next_redirects_there_instead_of_the_settings_page(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    r = signed_in_client(app, follow_redirects=False).post("/settings/companies/add", data={
        "family": "greenhouse", "name": "Acme", "identity": '{"slug": "acme"}',
        "next": "/wizard",
    })
    assert r.status_code == 303
    assert r.headers["location"] == "/wizard"
    assert app.state.service.snapshot().cfg.sources.greenhouse == ["acme"]


def test_add_without_next_keeps_the_default_settings_redirect(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    r = signed_in_client(app, follow_redirects=False).post("/settings/companies/add", data={
        "family": "greenhouse", "name": "Acme", "identity": '{"slug": "acme"}'})
    assert r.status_code == 303
    assert r.headers["location"] == "/settings/companies?added=1"


def test_add_rejects_an_off_site_next(tmp_path, monkeypatch):
    """The hidden `next` field survives in the browser's DOM and could be
    resubmitted with anything — safe_next() must be enforced here, not just
    trusted because companies_probe originally validated it. safe_next()'s
    own contract (src/web/auth.py) is to fall back to "/" for anything that
    isn't a same-site path, not to the companies page specifically — reused
    as-is here rather than re-implemented, so this pins that contract, not a
    companies-specific one."""
    app = _app(tmp_path, monkeypatch)
    r = signed_in_client(app, follow_redirects=False).post("/settings/companies/add", data={
        "family": "greenhouse", "name": "Acme", "identity": '{"slug": "acme"}',
        "next": "https://evil.example/steal",
    })
    assert r.status_code == 303
    assert r.headers["location"] == "/"


def test_add_is_idempotent_and_still_honours_next(tmp_path, monkeypatch):
    """The already-configured dedupe branch is a separate return point from
    the normal write path — it must honour `next` too, or re-confirming an
    already-added board from the wizard would strand the user exactly like
    the bug this fix targets."""
    app = _app(tmp_path, monkeypatch, make_service({"sources": {"greenhouse": ["acme"]}}))
    r = signed_in_client(app, follow_redirects=False).post("/settings/companies/add", data={
        "family": "greenhouse", "name": "Acme", "identity": '{"slug": "acme"}',
        "next": "/wizard",
    })
    assert r.status_code == 303
    assert r.headers["location"] == "/wizard"


def test_an_unsupported_jsonld_family_points_at_its_real_manual_form(tmp_path, monkeypatch):
    """icims/successfactors/talentbrew are hand-curated into jsonld_boards
    (fingerprint.py's own docstring) — a generic "/settings/rows/" substring
    match (the brief's own bamboohr test) would still pass a broken link to
    /settings/rows/sources.icims/new, which 404s: there is no sources.icims
    field."""
    _stub(monkeypatch, FingerprintResult(
        name="Acme", domain="acme.com", status="unsupported", family="icims",
        evidence_url="https://careers-acme.icims.com"))
    r = signed_in_client(_app(tmp_path, monkeypatch)).post(
        "/settings/companies/probe", data={"target": "acme.com"})
    assert "/settings/rows/sources.jsonld_boards/new" in r.text
