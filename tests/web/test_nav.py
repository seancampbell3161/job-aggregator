import pytest

from src.web.nav import NAV, NAV_FOOTER, active_href
from tests.web.shell_helpers import client_for, make_app


@pytest.mark.parametrize("path, expected", [
    ("/", "/"),
    ("/jobs", "/"),
    ("/detail", "/"),
    ("/home", "/home"),
    ("/board", "/board"),
    ("/boardx", None),            # prefix match is at a "/" boundary
    ("/settings", "/settings"),
    ("/settings/filters", "/settings"),
    ("/account/password", "/account/password"),
    ("/tailor/abc", None),        # deep-link only
    ("/pipeline", "/pipeline"),
    ("/audit", "/audit"),
    ("/nope", None),
])
def test_active_href(path, expected):
    assert active_href(path) == expected


def test_labels_in_order():
    labels = [(g.label, [i.label for i in g.items]) for g in NAV]
    assert labels == [
        (None, ["Home"]),
        ("Job search", ["Matches", "Applications", "Apply kit"]),
        ("Résumé", ["Résumé templates"]),
        ("Insights", ["Progress", "Coach", "System health", "Rejected postings"]),
    ]
    assert [i.label for i in NAV_FOOTER] == ["Settings", "Account"]


def test_every_nav_href_is_a_registered_get_route(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    gets = {r.path for r in app.routes if "GET" in (getattr(r, "methods", None) or set())}
    hrefs = [i.href for g in NAV for i in g.items] + [i.href for i in NAV_FOOTER]
    assert [h for h in hrefs if h not in gets] == []


def test_coach_link_follows_coach_nav_visible(tmp_path, monkeypatch):
    import src.web.coach as coach
    app = make_app(tmp_path, monkeypatch)
    monkeypatch.setattr(coach, "coach_nav_visible", lambda request: False)
    assert 'href="/coach"' not in client_for(app).get("/home").text
    monkeypatch.setattr(coach, "coach_nav_visible", lambda request: True)
    assert 'href="/coach"' in client_for(app).get("/home").text
