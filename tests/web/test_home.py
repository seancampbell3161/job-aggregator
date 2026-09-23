"""Home: getting-started checklist, the landing rule, hide/show."""
from src.web.home import HIDDEN_KEY
from tests.web.shell_helpers import client_for, finish_wizard, make_app, seed_cycle, seed_match

ALERTS = {"ntfy_topic_url": "https://ntfy.sh/x"}


def _done_keys(app):
    """The checklist keys the page marks done (data-key on each <li>)."""
    import re
    html = client_for(app).get("/home").text
    return set(re.findall(r'<li class="done" data-key="(\w+)"', html))


def test_fresh_instance_has_nothing_done(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    assert _done_keys(app) == set()


def test_each_item_flips_from_its_own_data(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch, secrets=ALERTS)
    assert _done_keys(app) == {"alerts"}
    finish_wizard(app)
    assert _done_keys(app) == {"alerts", "search"}
    seed_cycle(app, minutes_ago=5)
    assert _done_keys(app) == {"alerts", "search", "first_check"}
    seed_match(app, "a:1")
    assert "review" not in _done_keys(app)          # still "new"
    seed_match(app, "a:2", status="dismissed")
    assert _done_keys(app) == {"alerts", "search", "first_check", "review"}


def test_first_check_needs_a_successful_cycle(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    seed_cycle(app, minutes_ago=5, ok=False)
    assert "first_check" not in _done_keys(app)


def test_review_is_muted_until_the_first_check(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    html = client_for(app).get("/home").text
    assert '<li class="blocked" data-key="review">' in html
    assert "after the first check" in html
    seed_cycle(app, minutes_ago=5)
    html = client_for(app).get("/home").text
    assert 'data-key="review"' in html and '<li class="blocked" data-key="review">' not in html
    assert 'href="/">Open Matches</a>' in html


def test_one_failing_derivation_only_marks_that_item_undone(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch, secrets=ALERTS)
    finish_wizard(app)

    def boom():
        raise RuntimeError("locked")
    monkeypatch.setattr(app.state.repo, "status_counts", boom)
    r = client_for(app).get("/home")
    assert r.status_code == 200
    assert _done_keys(app) == {"alerts", "search"}


def test_hide_and_show_round_trip(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    c = client_for(app, follow_redirects=False)
    r = c.post("/home/getting-started/hide")
    assert r.status_code == 303 and r.headers["location"] == "/home"
    assert app.state.stores.wizard.get(HIDDEN_KEY) is True
    html = c.get("/home").text
    assert 'action="/home/getting-started/show"' in html
    assert 'data-key="search"' not in html
    r = c.post("/home/getting-started/show")
    assert r.status_code == 303
    assert app.state.stores.wizard.get(HIDDEN_KEY) is None
    assert 'data-key="search"' in c.get("/home").text


def test_hide_is_instance_wide(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    client_for(app).post("/home/getting-started/hide")
    other = client_for(app)   # a different browser: fresh cookie jar
    assert 'action="/home/getting-started/show"' in other.get("/home").text


def test_complete_checklist_says_all_set(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch, secrets=ALERTS)
    finish_wizard(app)
    seed_cycle(app, minutes_ago=5)
    seed_match(app, "a:1", status="applied")
    assert "from now on you'll land on Matches" in client_for(app).get("/home").text


def test_wizard_done_points_at_home(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    html = client_for(app).get("/wizard/done").text
    assert 'href="/home">Go to Home</a>' in html
    assert "Go to Overview" not in html
