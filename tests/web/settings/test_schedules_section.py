"""Cadences and cron fields."""
from src.web.app import create_app
from tests.auth_helpers import signed_in_client
from tests.settings_helpers import WEB_TEST_SETTINGS, make_service


def _app(tmp_path, monkeypatch, service=None):
    monkeypatch.setenv("JOB_AGG_SQLITE_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("JOB_AGG_TAILORED_DIR", str(tmp_path / "tailored"))
    return create_app(service=service if service is not None else make_service(WEB_TEST_SETTINGS))


FORM = {
    "schedules.ats_minutes": "10",
    "schedules.slow_minutes": "15",
    "schedules.discovery_hours": "24",
    "schedules.headless_minutes": "45",
    "schedules.digest_cron": "0 13 * * 1",
    "board.closed_check_cron": "30 4 * * *",
    "board.digest_cron": "0 15 * * *",
    "gmail.check_cron": "0 * * * *",
}


def test_page_renders(tmp_path, monkeypatch):
    r = signed_in_client(_app(tmp_path, monkeypatch)).get("/settings/schedules")
    assert r.status_code == 200
    assert 'name="schedules.ats_minutes"' in r.text
    assert 'name="board.digest_cron"' in r.text


def test_save_round_trips(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    signed_in_client(app).post("/settings/schedules",
                               data={**FORM, "schedules.ats_minutes": "5"})
    assert app.state.service.snapshot().cfg.schedules.ats_minutes == 5


def test_a_bad_crontab_renders_inline_on_its_own_field(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    r = signed_in_client(app).post("/settings/schedules",
                                   data={**FORM, "board.digest_cron": "not a cron"})
    assert r.status_code == 200
    # Bound the assertion to board.digest_cron's own field div, not just "the
    # error text is on the page somewhere": a plain substring check on
    # "board.digest_cron" can never fail, because that name= attribute is on
    # the page whether the error renders inline or in the form-level banner
    # (settings_base.html renders form_errors outside every field div).
    # Requiring has-error/field-error inside this specific div is what
    # actually distinguishes the two.
    idx = r.text.index('name="board.digest_cron"')
    div_start = r.text.rindex('<div class="field', 0, idx)
    div_end = r.text.index("</div>", idx)
    field_html = r.text[div_start:div_end]
    assert "has-error" in field_html
    assert "field-error" in field_html
    assert "invalid crontab" in field_html
    assert app.state.service.snapshot().cfg.board.digest_cron == "0 15 * * *"


def test_below_minimum_cadence_is_rejected(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    r = signed_in_client(app).post("/settings/schedules",
                                   data={**FORM, "schedules.ats_minutes": "0"})
    assert "greater than or equal to 1" in r.text
    assert app.state.service.snapshot().cfg.schedules.ats_minutes == 10
