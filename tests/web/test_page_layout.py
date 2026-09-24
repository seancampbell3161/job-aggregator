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
