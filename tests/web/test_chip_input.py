"""The chip input is JS (no JS test runner — behaviour is verified in a real
browser, see the plan's Task 7). These pin the server side of the contract:
the script is served and loaded, the old inline helpers are gone, and the
no-JS markup still posts a blank for an emptied list."""
import re
from pathlib import Path

from src.web.app import create_app
from tests.auth_helpers import signed_in_client
from tests.settings_helpers import WEB_TEST_SETTINGS, make_service

ROOT = Path(__file__).resolve().parents[2]


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("JOB_AGG_SQLITE_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("JOB_AGG_TAILORED_DIR", str(tmp_path / "tailored"))
    return signed_in_client(create_app(service=make_service(WEB_TEST_SETTINGS)))


def test_every_page_loads_the_chip_script(tmp_path, monkeypatch):
    html = _client(tmp_path, monkeypatch).get("/settings/filters").text
    assert '<script src="/static/js/chips.js" defer></script>' in html


def test_the_chip_script_is_served(tmp_path, monkeypatch):
    r = _client(tmp_path, monkeypatch).get("/static/js/chips.js")
    assert r.status_code == 200
    assert "chip-entry" in r.text


def test_the_inline_chip_helpers_are_gone():
    base = (ROOT / "src/web/templates/base.html").read_text()
    assert "function addChip" not in base
    assert "addChipValues" not in base


def test_no_add_another_button(tmp_path, monkeypatch):
    html = _client(tmp_path, monkeypatch).get("/settings/filters").text
    assert "add another" not in html
    assert "addChip(" not in html


def test_the_fallback_box_still_posts_a_blank(tmp_path, monkeypatch):
    """The present-but-blank contract (forms.decode): a chips path that is
    posted blank clears the list; one that is absent is left alone. The
    server markup must always carry a named blank input."""
    html = _client(tmp_path, monkeypatch).get("/settings/filters").text
    box = re.search(r'<div class="chips" data-path="filters\.titles">.*?</div>', html, re.S).group(0)
    assert '<input type="text" name="filters.titles" value="" placeholder="add one…">' in box


def test_the_script_keeps_the_entry_named():
    """Refinement 1: the enhanced entry box must keep the field's name, or an
    emptied list posts nothing and silently keeps its old values."""
    js = (ROOT / "src/web/static/js/chips.js").read_text()
    assert "entry.name = path" in js
