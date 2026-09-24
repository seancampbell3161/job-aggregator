"""The shared component macros (_ui.html), rendered through the app's real
Jinja environment so autoescaping behaves exactly as it does on a page."""
import pytest
from markupsafe import Markup

from src.web.app import create_app
from tests.settings_helpers import WEB_TEST_SETTINGS, make_service


@pytest.fixture
def ui(tmp_path, monkeypatch):
    monkeypatch.setenv("JOB_AGG_SQLITE_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("JOB_AGG_TAILORED_DIR", str(tmp_path / "tailored"))
    app = create_app(service=make_service(WEB_TEST_SETTINGS))
    return app.state.templates.env.get_template("_ui.html").module


def body():
    return Markup('<p id="body">body</p>')


def control():
    return Markup('<input id="c">')


# ---- page_header ----

def test_page_header_title_only(ui):
    html = str(ui.page_header("Board"))
    assert '<h1 class="page-title">Board</h1>' in html
    assert "page-subtitle" not in html
    assert "page-actions" not in html


def test_page_header_subtitle_and_actions(ui):
    html = str(ui.page_header("Board", "Jobs by stage", caller=body))
    assert '<p class="page-subtitle">Jobs by stage</p>' in html
    assert '<div class="page-actions"><p id="body">body</p></div>' in html


def test_page_header_escapes_title(ui):
    html = str(ui.page_header("<b>x</b>"))
    assert "<b>" not in html and "&lt;b&gt;" in html


# ---- card ----

def test_card_renders_title_subtitle_and_body(ui):
    html = str(ui.card("Phone alerts", "Delivered by ntfy", caller=body))
    assert html.startswith('<section class="card">')
    assert '<h2 class="card-title">Phone alerts</h2>' in html
    assert '<p class="card-subtitle">Delivered by ntfy</p>' in html
    assert '<p id="body">body</p>' in html
    assert html.endswith("</section>")


def test_card_without_title_has_no_heading(ui):
    html = str(ui.card(caller=body))
    assert "card-title" not in html
    assert "card-subtitle" not in html
    assert '<p id="body">body</p>' in html


def test_card_extra_class_and_tag(ui):
    html = str(ui.card(extra_class="accent-edge", tag="div", caller=body))
    assert html.startswith('<div class="card accent-edge">')
    assert html.endswith("</div>")


# ---- alert ----

@pytest.mark.parametrize("kind,role", [
    ("ok", "status"), ("warn", "status"), ("info", "status"), ("bad", "alert")])
def test_alert_kind_and_role(ui, kind, role):
    assert str(ui.alert(kind, "Saved.")) == f'<div class="alert {kind}" role="{role}">Saved.</div>'


def test_alert_escapes_its_message(ui):
    html = str(ui.alert("bad", "<script>alert(1)</script>"))
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_alert_with_body_and_extra_class(ui):
    html = str(ui.alert("warn", extra_class="pre-line", caller=body))
    assert html == '<div class="alert warn pre-line" role="status"><p id="body">body</p></div>'


# ---- status_pill ----

def test_status_pill_capitalizes_by_default(ui):
    assert str(ui.status_pill("interviewing")) == '<span class="pill st-interviewing">Interviewing</span>'


def test_status_pill_explicit_label(ui):
    assert str(ui.status_pill("new", "Fresh")) == '<span class="pill st-new">Fresh</span>'


# ---- field ----

def test_field_minimal(ui):
    html = str(ui.field("Minimum score", control_id="c", caller=control))
    assert '<div class="field">' in html
    assert '<label class="field-label" for="c">Minimum score</label>' in html
    assert '<input id="c">' in html
    for absent in ("field-hint", "field-meta", "field-req", "field-error",
                   "field-required-why", "has-error", "data-path"):
        assert absent not in html, absent


def test_field_full_anatomy_in_order(ui):
    html = str(ui.field(
        "Topic URL", control_id="c", hint="Anyone with it can read alerts.",
        required="Alerts need somewhere to go.", error="Not a URL.",
        path="secrets.ntfy", caller=control))
    assert '<div class="field has-error" data-path="secrets.ntfy">' in html
    assert '<span class="field-req">required</span>' in html
    assert (html.index("field-label") < html.index("field-hint") < html.index('<input id="c">')
            < html.index("field-required-why") < html.index("field-error"))
    assert '<p class="field-required-why">Alerts need somewhere to go.</p>' in html
    assert '<p class="field-error" id="c-error">Not a URL.</p>' in html


def test_field_required_tag_replaces_meta(ui):
    html = str(ui.field("Titles", control_id="c", meta="default 6", required="why", caller=control))
    assert "field-req" in html
    assert "field-meta" not in html


def test_field_meta_shown_when_not_required(ui):
    html = str(ui.field("Min", control_id="c", meta="default 6", caller=control))
    assert '<span class="field-meta">default 6</span>' in html


def test_field_required_without_reason_marks_but_explains_nothing(ui):
    html = str(ui.field("Titles", required="", caller=control))
    assert "field-req" in html
    assert "field-required-why" not in html


def test_field_required_without_label_still_shows_the_tag(ui):
    html = str(ui.field(control_id="c", required="", caller=control))
    assert '<span class="field-req">required</span>' in html
    assert "field-label" not in html


def test_field_without_control_id_uses_a_span_label_and_unnamed_error(ui):
    html = str(ui.field("Workplace", error="Pick one.", caller=control))
    assert '<span class="field-label">Workplace</span>' in html
    assert "<label" not in html
    assert '<p class="field-error">Pick one.</p>' in html


def test_field_hint_markup_passes_through_but_strings_escape(ui):
    trusted = str(ui.field("A", hint=Markup("<p>ok</p>"), caller=control))
    assert '<div class="field-hint"><p>ok</p></div>' in trusted
    untrusted = str(ui.field("A", hint="<b>x</b>", caller=control))
    assert "<b>x</b>" not in untrusted and "&lt;b&gt;x&lt;/b&gt;" in untrusted


def test_field_error_is_escaped(ui):
    html = str(ui.field("A", control_id="c", error="<img src=x>", caller=control))
    assert "<img" not in html


# ---- empty_state ----

def test_empty_state_title_only(ui):
    html = str(ui.empty_state("No matches."))
    assert '<p class="empty-state-title">No matches.</p>' in html
    for absent in ("empty-state-icon", "empty-state-body", "<a "):
        assert absent not in html, absent


def test_empty_state_full(ui):
    html = str(ui.empty_state("No matches yet", "The first check runs within the hour.",
                              icon="◎", action_href="/pipeline", action_label="View pipeline"))
    assert '<div class="empty-state-icon" aria-hidden="true">◎</div>' in html
    assert '<p class="empty-state-body">The first check runs within the hour.</p>' in html
    assert '<a class="btn sm" href="/pipeline">View pipeline</a>' in html


def test_empty_state_needs_both_href_and_label_for_an_action(ui):
    assert "<a " not in str(ui.empty_state("x", action_href="/p"))
    assert "<a " not in str(ui.empty_state("x", action_label="Go"))


def test_empty_state_caller_renders_actions(ui):
    html = str(ui.empty_state("x", caller=body))
    assert '<div class="empty-state-actions"><p id="body">body</p></div>' in html
    assert "empty-state-actions" not in str(ui.empty_state("x"))


# ---- readiness_list ----

def test_readiness_list(ui):
    from src.web.settings.readiness import Warning
    html = str(ui.readiness_list([Warning("no_titles", "Add a title.", "filters")]))
    assert '<li class="warn"><span>Add a title.</span>' in html
    assert '<a class="btn sm" href="/settings/filters">Fix</a>' in html


# ---- action_bar ----

def test_action_bar_with_back(ui):
    html = str(ui.action_bar(back_href="/wizard/llm", caller=body))
    assert html.startswith('<div class="action-bar">')
    assert '<a class="btn ghost" href="/wizard/llm">Back</a>' in html
    assert '<div class="action-bar-end"><p id="body">body</p></div>' in html
    assert html.index("Back") < html.index("action-bar-end")


def test_action_bar_without_back(ui):
    html = str(ui.action_bar(caller=body))
    assert "Back" not in html and "<a " not in html
    assert '<div class="action-bar-end"><p id="body">body</p></div>' in html


def test_action_bar_escapes_back_href(ui):
    html = str(ui.action_bar(back_href='/x"><script>', caller=body))
    assert "<script>" not in html


def test_action_bar_is_pinned_by_default(ui):
    html = str(ui.action_bar(caller=body))
    assert html.startswith('<div class="action-bar">')
    assert '<div class="action-bar-end"><p id="body">body</p></div>' in html


def test_action_bar_can_be_unpinned(ui):
    html = str(ui.action_bar(sticky=False, caller=body))
    assert html.startswith('<div class="action-bar unpinned">')


# ---- field group ----

def test_field_group_is_a_fieldset_with_a_legend(ui):
    html = str(ui.field(label="What level?", hint="Pick any", group=True, path="f.x", caller=control))
    assert html.startswith('<fieldset class="field" data-path="f.x">')
    assert '<legend class="field-label">What level?</legend>' in html
    assert html.endswith("</fieldset>")


def test_field_without_group_is_unchanged(ui):
    html = str(ui.field(label="Name", control_id="c", caller=control))
    assert html.startswith('<div class="field">')
    assert '<label class="field-label" for="c">Name</label>' in html
