"""Sub-project D1 (page layout and consistency): the server-side contract of
the layout changes. Visual behaviour is checked in a real browser (plan
Task 6); these pin the markup that behaviour depends on."""
import re
from pathlib import Path

from tests.web.shell_helpers import client_for, make_app

TEMPLATES = Path(__file__).resolve().parents[2] / "src" / "web" / "templates"

# Edit forms whose save lives in ui.action_bar. The remove-confirm forms
# (row_remove.html, _company_remove.html) deliberately keep .actions.
_SAVE_BAR_TEMPLATES = sorted(TEMPLATES.glob("settings_*.html")) + [
    TEMPLATES / "row_form.html", TEMPLATES / "_content_draft_result.html"]


def test_settings_forms_use_the_action_bar_not_actions():
    offenders = [p.name for p in _SAVE_BAR_TEMPLATES if 'class="actions"' in p.read_text()]
    assert not offenders, f"use ui.action_bar() instead of .actions: {offenders}"


def test_settings_section_saves_from_the_pinned_bar(tmp_path, monkeypatch):
    html = client_for(make_app(tmp_path, monkeypatch)).get("/settings/filters").text
    bar = re.search(r'<div class="action-bar">.*?</div>\s*</div>', html, re.S)
    assert bar and "Save filters" in bar.group(0)


def test_backup_restore_bar_is_not_pinned(tmp_path, monkeypatch):
    html = client_for(make_app(tmp_path, monkeypatch)).get("/settings/backup").text
    assert '<div class="action-bar unpinned">' in html
    assert '<div class="action-bar">' not in html


def test_llm_probe_result_sits_outside_the_bar(tmp_path, monkeypatch):
    html = client_for(make_app(tmp_path, monkeypatch)).get("/settings/llm").text
    bar = re.search(r'<div class="action-bar">.*?</div>\s*</div>', html, re.S)
    assert bar and "Test scoring" in bar.group(0)
    assert 'id="probe-llm"' not in bar.group(0)
    assert 'id="probe-llm"' in html


# Tables wider than a 390px screen: each must sit directly in a .table-wrap,
# so it scrolls inside its card instead of dragging the page sideways.
_WIDE_TABLE_TEMPLATES = ["_ops_cycles.html", "_ops_health.html", "audit.html", "settings_history.html"]


def test_wide_tables_are_wrapped():
    for name in _WIDE_TABLE_TEMPLATES:
        src = (TEMPLATES / name).read_text()
        tables = len(re.findall(r"<table\b", src))
        wrapped = len(re.findall(r'<div class="table-wrap">\s*<table\b', src))
        assert tables and wrapped == tables, f"{name}: {wrapped}/{tables} tables wrapped"


def test_company_tables_share_one_column_layout():
    src = (TEMPLATES / "_company_rows.html").read_text()
    assert '<table class="company-table">' in src
    assert re.search(r'<colgroup>\s*<col class="c-board">\s*<col class="c-detail">\s*'
                     r'<col class="c-status">\s*<col class="c-actions">\s*</colgroup>', src)


def test_numeric_cells_are_marked(tmp_path, monkeypatch):
    from tests.web.shell_helpers import seed_cycle, seed_match
    app = make_app(tmp_path, monkeypatch)
    seed_match(app, "j1")
    seed_cycle(app, minutes_ago=5)
    c = client_for(app)
    assert '<td class="num">' in c.get("/analytics").text
    assert '<td class="num">' in c.get("/pipeline/cycles").text


def _builder_client(tmp_path, monkeypatch):
    monkeypatch.setenv("JOB_AGG_TEMPLATES_DIR", str(tmp_path / "templates"))
    return client_for(make_app(tmp_path, monkeypatch))


def test_builder_settings_use_stacked_fields(tmp_path, monkeypatch):
    html = _builder_client(tmp_path, monkeypatch).get("/builder").text
    for name in ("max_bullets_per_experience", "max_bullets_per_project", "min_bullets_per_entry",
                 "max_pages", "page_size", "margins"):
        m = re.search(rf'<(?:input|select)[^>]*\bid="(b-[\w-]+)"[^>]*\bname="{name}"', html)
        assert m, f"{name} has no id"
        assert f'<label class="field-label" for="{m.group(1)}">' in html
    assert '<div class="field-hint">CSS, e.g. 0.5in 0.58in</div>' in html
    assert '<div class="action-bar unpinned">' in html


def test_audit_days_input_is_labelled(tmp_path, monkeypatch):
    html = client_for(make_app(tmp_path, monkeypatch)).get("/audit").text
    assert re.search(r'<label class="audit-days-field">last\s*<input[^>]*name="days"[^>]*>\s*days</label>', html)
