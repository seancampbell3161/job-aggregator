"""The shared field macros (`_settings_macros.html`) that every section page
(Tasks 6-12) renders through. Rendered via the app's real Jinja environment —
the one `register_settings_routes` registers `field_help`/`value_at` on and
that has autoescaping configured the same way production does — so these
tests exercise the exact behaviour a browser would see, not an approximation."""
from src.settings.fields import field_map
from src.web.app import create_app
from tests.auth_helpers import signed_in_client
from tests.settings_helpers import WEB_TEST_SETTINGS, make_service


def _macros(tmp_path, monkeypatch):
    """The compiled `_settings_macros.html` module, off a real app's Jinja
    environment (same loader, same autoescape, same registered globals as
    every section page will use)."""
    monkeypatch.setenv("JOB_AGG_SQLITE_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("JOB_AGG_TAILORED_DIR", str(tmp_path / "tailored"))
    app = create_app(service=make_service(WEB_TEST_SETTINGS))
    return app.state.templates.env.get_template("_settings_macros.html").module


def test_multi_choice_tuple_default_is_not_rendered(tmp_path, monkeypatch):
    """Correction 1: FieldSpec.default is a tuple for list-typed fields;
    the field-meta span must suppress it for multi_choice, not just chips."""
    spec = field_map()["filters.seniority_allow"]
    assert spec.kind == "multi_choice"
    assert spec.default == ("mid", "senior")  # pins the tuple-default fact
    html = str(_macros(tmp_path, monkeypatch).field(spec, ["mid", "senior"], {}))
    assert "default" not in html
    assert "('mid', 'senior')" not in html


def test_chips_value_is_autoescaped(tmp_path, monkeypatch):
    """A chip value containing markup must never reach the page unescaped —
    the injection hole this plan has already shipped once elsewhere."""
    spec = field_map()["filters.titles"]
    assert spec.kind == "chips"
    payload = '"><script>alert(1)</script>'
    html = str(_macros(tmp_path, monkeypatch).field(spec, [payload], {}))
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_help_disclosure_appears_when_full_differs_from_summary(tmp_path, monkeypatch):
    """Correction 2: the <details>/<summary> disclosure, no onclick JS."""
    html = str(_macros(tmp_path, monkeypatch).help_for("filters.max_age_days"))
    assert "<details" in html
    assert "<summary>more</summary>" in html
    assert "onclick" not in html


def test_help_disclosure_is_omitted_when_full_equals_summary(tmp_path, monkeypatch):
    html = str(_macros(tmp_path, monkeypatch).help_for("filters.seniority_allow"))
    assert "<details" not in html


def test_only_true_secrets_are_masked(tmp_path, monkeypatch):
    """secret_field masks by name (_key/_password/_secret) rather than masking
    every secret input unconditionally — an API key stays type="password" (its
    stored value never renders, so masking only affects what's visible while
    typing/pasting it), but a pasted URL like ntfy_topic_url renders
    type="text" so it can be proofread before submit; the integrations page
    has no Test button, so a typo in a plain-text field like that would
    otherwise fail silently until the feature breaks. Exercised through the
    real app on two pages (llm, notifications) that both render through the
    shared secret_field macro, so this catches a regression in either
    secret_rows()'s masked flag or the template's use of it."""
    monkeypatch.setenv("JOB_AGG_SQLITE_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("JOB_AGG_TAILORED_DIR", str(tmp_path / "tailored"))
    app = create_app(service=make_service(WEB_TEST_SETTINGS))
    client = signed_in_client(app)

    llm_html = client.get("/settings/llm").text
    key_idx = llm_html.index('name="secret.anthropic_api_key"')
    assert 'type="password"' in llm_html[key_idx - 60:key_idx]

    notif_html = client.get("/settings/notifications").text
    url_idx = notif_html.index('name="secret.ntfy_topic_url"')
    assert 'type="text"' in notif_html[url_idx - 60:url_idx]
    assert 'type="password"' not in notif_html[url_idx - 60:url_idx]


def _int_with_default():
    """An int field that has a default, so its label row carries meta."""
    return next(s for s in field_map().values() if s.kind == "int" and s.default is not None)


def test_field_error_is_wired_to_its_control(tmp_path, monkeypatch):
    spec = field_map()["filters.max_age_days"]
    assert spec.kind == "int"
    html = str(_macros(tmp_path, monkeypatch).field(spec, 5, {spec.path: "too big"}))
    cid = f"f-{spec.path}"
    assert f'id="{cid}"' in html
    assert 'aria-invalid="true"' in html
    assert f'aria-describedby="{cid}-error"' in html
    assert f'<p class="field-error" id="{cid}-error">too big</p>' in html
    assert "has-error" in html


def test_a_valid_field_carries_no_error_wiring(tmp_path, monkeypatch):
    spec = field_map()["filters.max_age_days"]
    html = str(_macros(tmp_path, monkeypatch).field(spec, 5, {}))
    assert "aria-invalid" not in html
    assert "field-error" not in html
    assert "has-error" not in html


def test_the_hint_renders_above_the_control(tmp_path, monkeypatch):
    spec = field_map()["filters.max_age_days"]   # has help text (see the disclosure test above)
    html = str(_macros(tmp_path, monkeypatch).field(spec, 5, {}))
    assert html.index('class="field-hint"') < html.index(f'id="f-{spec.path}"')


def test_the_default_sits_in_the_label_row(tmp_path, monkeypatch):
    spec = _int_with_default()
    html = str(_macros(tmp_path, monkeypatch).field(spec, None, {}))
    assert f'<span class="field-meta">default {spec.default}</span>' in html
    assert html.index("field-meta") < html.index("</label>")


def test_a_bool_field_is_a_checkbox_label_not_a_field_label(tmp_path, monkeypatch):
    spec = next(s for s in field_map().values() if s.kind == "bool")
    html = str(_macros(tmp_path, monkeypatch).field(spec, True, {}))
    assert '<label class="check">' in html
    assert "field-label" not in html
    assert f'id="f-{spec.path}"' in html and "checked" in html


def test_a_multi_choice_field_labels_the_group_not_a_control(tmp_path, monkeypatch):
    spec = field_map()["filters.seniority_allow"]
    html = str(_macros(tmp_path, monkeypatch).field(spec, ["mid"], {}))
    assert '<span class="field-label">' in html
    assert f'for="f-{spec.path}"' not in html   # there is no single control to point at
