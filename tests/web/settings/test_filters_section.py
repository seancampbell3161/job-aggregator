"""Saving a section: the patch path, inline errors, and StaleWrite."""
import pytest

from src.settings.errors import StaleWrite
from src.web.app import create_app
from tests.auth_helpers import signed_in_client
from tests.settings_helpers import WEB_TEST_SETTINGS, make_service


def _app(tmp_path, monkeypatch, service=None):
    monkeypatch.setenv("JOB_AGG_SQLITE_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("JOB_AGG_TAILORED_DIR", str(tmp_path / "tailored"))
    return create_app(service=service if service is not None else make_service(WEB_TEST_SETTINGS))


FORM = {
    "filters.titles": ["platform engineer", ""],
    "filters.seniority_allow": ["mid", "senior"],
    "filters.stack_any_of": [""],
    "filters.comp_floor_usd": "180000",
    "filters.max_age_days": "2",
    "filters.blocked_employment_types": ["contract"],
    "filters.blocked_companies": [""],
    "filters.location.allowed_countries": ["US"],
    "filters.location.allowed_cities": [""],
    "filters.location.remote_policy": "allowed_countries",
    "filters.location.allow_unknown": "on",
}


def test_page_renders_current_values(tmp_path, monkeypatch):
    service = make_service({"filters": {"titles": ["staff engineer"], "comp_floor_usd": 210000}})
    r = signed_in_client(_app(tmp_path, monkeypatch, service)).get("/settings/filters")
    assert r.status_code == 200
    assert 'name="filters.titles"' in r.text
    assert "staff engineer" in r.text
    assert "210000" in r.text


def test_save_writes_a_patch(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    r = signed_in_client(app).post("/settings/filters", data=FORM)
    assert r.status_code == 200
    cfg = app.state.service.snapshot().cfg
    assert cfg.filters.titles == ["platform engineer"]      # blank dropped
    assert cfg.filters.comp_floor_usd == 180000
    assert cfg.filters.max_age_days == 2
    assert cfg.filters.seniority_allow == ["mid", "senior"]


def test_save_leaves_other_sections_untouched(tmp_path, monkeypatch):
    service = make_service({"relevance": {"score_low": 4}, "discovery": {"enabled": True}})
    app = _app(tmp_path, monkeypatch, service)
    signed_in_client(app).post("/settings/filters", data=FORM)
    cfg = app.state.service.snapshot().cfg
    assert cfg.relevance.score_low == 4
    assert cfg.discovery.enabled is True


def test_unchecking_a_box_turns_it_off(tmp_path, monkeypatch):
    service = make_service({"filters": {"location": {"allow_unknown": True}}})
    app = _app(tmp_path, monkeypatch, service)
    form = {k: v for k, v in FORM.items() if k != "filters.location.allow_unknown"}
    signed_in_client(app).post("/settings/filters", data=form)
    assert app.state.service.snapshot().cfg.filters.location.allow_unknown is False


def test_setting_a_value_back_to_its_default_removes_it_from_the_doc(tmp_path, monkeypatch):
    service = make_service({"filters": {"comp_floor_usd": 180000}})
    app = _app(tmp_path, monkeypatch, service)
    signed_in_client(app).post("/settings/filters", data={**FORM, "filters.comp_floor_usd": "0"})
    _, doc = app.state.service.current_doc()
    assert "comp_floor_usd" not in doc.get("filters", {})
    assert app.state.service.snapshot().cfg.filters.comp_floor_usd == 0


def test_invalid_value_renders_inline_and_saves_nothing(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    before = app.state.service.current_config()[0]
    r = signed_in_client(app).post(
        "/settings/filters", data={**FORM, "filters.comp_floor_usd": "-5"})
    assert r.status_code == 200
    assert "filters.comp_floor_usd" in r.text
    assert "greater than or equal to 0" in r.text
    assert app.state.service.current_config()[0] == before  # no new version


def test_invalid_submission_keeps_what_was_typed(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    r = signed_in_client(app).post("/settings/filters", data={
        **FORM, "filters.comp_floor_usd": "-5", "filters.titles": ["kept typing"]})
    assert "kept typing" in r.text


def test_non_numeric_int_is_a_field_error(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    r = signed_in_client(app).post(
        "/settings/filters", data={**FORM, "filters.comp_floor_usd": "lots"})
    assert "must be a whole number" in r.text


def test_an_unchanged_submission_writes_no_version(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    c = signed_in_client(app)
    c.post("/settings/filters", data=FORM)
    after_first = app.state.service.current_config()[0]
    c.post("/settings/filters", data=FORM)
    assert app.state.service.current_config()[0] == after_first


def test_stale_write_is_reported_not_raised(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)

    def boom(mutate, *, source):
        raise StaleWrite("conflict")

    monkeypatch.setattr(app.state.service, "update_settings", boom)
    r = signed_in_client(app).post("/settings/filters", data=FORM)
    assert r.status_code == 200
    assert "saved while you were editing" in r.text
