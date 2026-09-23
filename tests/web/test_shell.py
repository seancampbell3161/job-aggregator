import re

import pytest

from tests.web.shell_helpers import client_for, make_app, seed_match

PAGES = ["/", "/home", "/board", "/kit", "/builder", "/analytics", "/pipeline", "/audit",
         "/settings/overview", "/account/password"]


@pytest.mark.parametrize("path", PAGES)
def test_exactly_one_current_item(tmp_path, monkeypatch, path):
    app = make_app(tmp_path, monkeypatch)
    html = client_for(app).get(path).text
    assert html.count('aria-current="page"') == 1, path


def test_current_item_is_the_right_one(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    html = client_for(app).get("/settings/filters").text
    assert re.search(r'<a class="nav-link current" href="/settings" aria-current="page">Settings', html)


def test_old_top_nav_is_gone(tmp_path, monkeypatch):
    html = client_for(make_app(tmp_path, monkeypatch)).get("/home").text
    assert "topnav" not in html and "site-header" not in html
    assert 'id="sidebar"' in html and 'href="/audit">Rejected postings' in html


def test_badge_shows_new_count_and_hides_at_zero(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    assert "nav-badge" not in client_for(app).get("/home").text
    seed_match(app, "a:1")
    seed_match(app, "a:2")
    seed_match(app, "a:3", status="applied")
    html = client_for(app).get("/home").text
    assert re.search(r'Matches<span class="nav-badge">2<span class="sr-only"> new</span></span>', html)


def test_badge_absent_and_page_ok_when_counts_raise(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)

    def boom():
        raise RuntimeError("database is locked")
    monkeypatch.setattr(app.state.repo, "status_counts", boom)
    for path in ("/home", "/board", "/settings/overview"):
        r = client_for(app).get(path)
        assert r.status_code == 200 and "nav-badge" not in r.text, path


def test_menu_toggle_controls_the_links(tmp_path, monkeypatch):
    html = client_for(make_app(tmp_path, monkeypatch)).get("/home").text
    assert 'aria-controls="sidebar-links"' in html and 'id="sidebar-links"' in html
    assert 'aria-controls="sidebar-links" aria-expanded="false"' in html


def test_brand_links_to_landing(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    assert '<a class="brand" href="/home">' in client_for(app).get("/board").text
    app.state.stores.wizard.put("getting_started_hidden", True)
    assert '<a class="brand" href="/">' in client_for(app).get("/board").text


def test_wizard_step_page_has_no_sidebar(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    r = client_for(app).get("/wizard", follow_redirects=True)   # lands on a step page
    assert r.url.path.startswith("/wizard/")
    assert 'id="sidebar"' not in r.text
    assert 'class="minimal-leave" href="/home"' in r.text


def test_setup_page_has_minimal_shell_without_leave_link(tmp_path, monkeypatch):
    monkeypatch.setenv("JOB_AGG_SQLITE_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("JOB_AGG_TAILORED_DIR", str(tmp_path / "tailored"))
    from src.web.app import create_app
    from tests.settings_helpers import make_service
    app = create_app(service=make_service(None))   # no settings version → not set up
    html = client_for(app).get("/setup").text
    assert 'id="sidebar"' not in html and "minimal-leave" not in html


def test_page_titles(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    assert "<title>Home · Job Aggregator</title>" in client_for(app).get("/home").text
