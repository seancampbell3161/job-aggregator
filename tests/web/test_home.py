"""Home: getting-started checklist, the landing rule, hide/show."""
import re

from src.web.home import COMPLETED_KEY, HIDDEN_KEY, HomeStatus, status_line
from src.web.ops import Liveness
from tests.web.shell_helpers import client_for, finish_wizard, make_app, seed_cycle, seed_match

ALERTS = {"ntfy_topic_url": "https://ntfy.sh/x"}


def _done_keys(app):
    """The checklist keys the page marks done (data-key on each <li>)."""
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


def test_first_check_counts_a_failed_cycle(tmp_path, monkeypatch):
    """A check that RAN counts: one persistently failing board keeps every
    cycle ok=0, and Home's status already says Running then."""
    app = make_app(tmp_path, monkeypatch)
    assert "first_check" not in _done_keys(app)
    seed_cycle(app, minutes_ago=5, ok=False)
    assert "first_check" in _done_keys(app)
    html = client_for(app).get("/home").text
    assert '<li class="blocked" data-key="review">' not in html


# Settings that satisfy every config-derived wizard step on their own: scoring
# on and keyed, titles + an age bound + a profile, a chosen board, a sink.
CONFIGURED = {
    "relevance": {"enabled": True, "provider": "anthropic", "score_high": 7, "score_low": 4},
    "filters": {"titles": ["Engineer"], "max_age_days": 7},
    "sources": {"greenhouse": ["acme"]},
}
CONFIGURED_SECRETS = {"anthropic_api_key": "sk-test", **ALERTS}
CONFIGURED_DOCS = {"resume_text": "My résumé", "profile": "What I want"}


def test_search_is_done_without_the_wizard_preview(tmp_path, monkeypatch):
    """An instance set up before the wizard existed (or via /setup/start +
    Settings, or /setup/restore) has no preview marker and no skips — its
    configuration alone completes "Set up your search"."""
    app = make_app(tmp_path, monkeypatch, settings=CONFIGURED,
                   secrets=CONFIGURED_SECRETS, documents=CONFIGURED_DOCS)
    assert app.state.stores.wizard.get("preview") is None
    assert not app.state.stores.wizard.skipped()
    assert "search" in _done_keys(app)


def test_search_still_needs_the_config_steps(tmp_path, monkeypatch):
    """Ignoring the preview step does not ignore the others: skipping only
    the preview leaves an unconfigured search undone."""
    app = make_app(tmp_path, monkeypatch)
    app.state.stores.wizard.skip("preview")
    assert "search" not in _done_keys(app)


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


def test_completion_is_latched(tmp_path, monkeypatch):
    """Once every item has been done, the card's promise holds: an item going
    undone later (here: the reviewed match disappears) doesn't bring the
    checklist back or send the brand link to Home."""
    app = make_app(tmp_path, monkeypatch, secrets=ALERTS)
    finish_wizard(app)
    seed_cycle(app, minutes_ago=5)
    seed_match(app, "a:1", status="applied")
    c = client_for(app)
    c.get("/home")
    assert app.state.stores.wizard.get(COMPLETED_KEY) is True
    app.state.stores.seen._conn.execute("DELETE FROM seen_jobs")
    html = c.get("/home").text
    assert "review" not in _done_keys(app)            # items still render live
    assert "from now on you'll land on Matches" in html
    assert '<a class="brand" href="/">' in c.get("/board").text


def test_incomplete_checklist_is_not_latched(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch, secrets=ALERTS)
    client_for(app).get("/home")
    assert app.state.stores.wizard.get(COMPLETED_KEY) is None


def test_latch_write_failure_degrades(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch, secrets=ALERTS)
    finish_wizard(app)
    seed_cycle(app, minutes_ago=5)
    seed_match(app, "a:1", status="applied")

    def boom(key, value):
        raise RuntimeError("locked")
    monkeypatch.setattr(app.state.stores.wizard, "put", boom)
    r = client_for(app).get("/home")
    assert r.status_code == 200 and "from now on you'll land on Matches" in r.text


def test_done_items_announce_done_to_screen_readers(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch, secrets=ALERTS)
    html = client_for(app).get("/home").text
    alerts = re.search(r'data-key="alerts">(.*?)</li>', html, re.S).group(1)
    search = re.search(r'data-key="search">(.*?)</li>', html, re.S).group(1)
    assert '<span class="sr-only">Done: </span>' in alerts
    assert "sr-only" not in search


def test_status_counts_is_read_once_per_request(tmp_path, monkeypatch):
    """The sidebar badge, the checklist and Home's figures all need the
    counts — one scan of seen_jobs per request, not one per caller."""
    app = make_app(tmp_path, monkeypatch)
    seed_cycle(app, minutes_ago=5)
    calls = []
    real = app.state.repo.status_counts

    def counting():
        calls.append(1)
        return real()
    monkeypatch.setattr(app.state.repo, "status_counts", counting)
    c = client_for(app)
    assert c.get("/home").status_code == 200
    assert len(calls) == 1
    calls.clear()
    c.get("/home")
    assert len(calls) == 1          # per request, not per process


def test_getting_started_is_derived_once_per_request(tmp_path, monkeypatch):
    """The brand link (landing_url) and Home's card share one derivation."""
    app = make_app(tmp_path, monkeypatch)
    calls = []
    real = app.state.stores.wizard.skipped

    def counting():
        calls.append(1)
        return real()
    monkeypatch.setattr(app.state.stores.wizard, "skipped", counting)
    assert client_for(app).get("/home").status_code == 200
    assert len(calls) == 1


def test_liveness_is_read_once_per_request(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    calls = []
    real = app.state.ops.liveness

    def counting():
        calls.append(1)
        return real()
    monkeypatch.setattr(app.state.ops, "liveness", counting)
    assert client_for(app).get("/home").status_code == 200
    assert len(calls) == 1


def test_a_failing_count_is_read_once_and_every_caller_degrades(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch, secrets=ALERTS)
    calls = []

    def boom():
        calls.append(1)
        raise RuntimeError("locked")
    monkeypatch.setattr(app.state.repo, "status_counts", boom)
    r = client_for(app).get("/home")
    assert r.status_code == 200
    assert len(calls) == 1


def test_wizard_done_points_at_home(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    html = client_for(app).get("/wizard/done").text
    assert 'href="/home">Go to Home</a>' in html
    assert "Go to Overview" not in html


# ---- status_line ----

NOW = 100_000_000_000
MIN = 60_000


def test_status_unavailable_without_telemetry():
    assert status_line(None, ats_minutes=30, now_ms=NOW).state == "unavailable"


def test_status_waiting_before_any_cycle():
    s = status_line(Liveness(None, None, None), ats_minutes=30, now_ms=NOW)
    assert s == HomeStatus("waiting", None, None, 30)


def test_status_running_predicts_next_ats_cycle():
    live = Liveness(last_cycle_ms=NOW - 12 * MIN, last_ats_ms=NOW - 12 * MIN, last_success_ms=NOW - 12 * MIN)
    s = status_line(live, ats_minutes=30, now_ms=NOW)
    assert s.state == "running" and s.last_ago == "12m ago" and s.next_eta == "in about 18 min"


def test_status_running_past_due_says_any_minute():
    live = Liveness(NOW - 40 * MIN, NOW - 40 * MIN, NOW - 40 * MIN)
    assert status_line(live, ats_minutes=30, now_ms=NOW).next_eta == "any minute now"


def test_status_boundary_matches_watchdog_threshold():
    from src.ops_alerts import stale_threshold_minutes
    t = stale_threshold_minutes(30) * MIN
    at = Liveness(NOW - t, NOW - t, NOW - t)
    past = Liveness(NOW - t - 1, NOW - t - 1, NOW - t - 1)
    assert status_line(at, ats_minutes=30, now_ms=NOW).state == "running"
    assert status_line(past, ats_minutes=30, now_ms=NOW).state == "stalled"


def test_status_uses_the_shared_threshold(monkeypatch):
    import src.web.home as home
    monkeypatch.setattr(home, "stale_threshold_minutes", lambda interval: 1)
    live = Liveness(NOW - 2 * MIN, NOW - 2 * MIN, NOW - 2 * MIN)
    assert status_line(live, ats_minutes=30, now_ms=NOW).state == "stalled"


def test_status_running_falls_back_to_last_cycle_when_ats_never_ran():
    """Only a slow-tier cycle has run yet (e.g. discovery, not ats) — the ETA
    anchor falls back to last_cycle_ms."""
    live = Liveness(last_cycle_ms=NOW - 5 * MIN, last_ats_ms=None, last_success_ms=NOW - 5 * MIN)
    s = status_line(live, ats_minutes=30, now_ms=NOW)
    assert s.state == "running" and s.last_ago == "5m ago" and s.next_eta == "in about 25 min"


def test_status_running_prefers_last_ats_over_last_cycle():
    """A non-ats tier cycle ran more recently than the ats tier — the ETA
    anchor is last_ats_ms, not the newer last_cycle_ms."""
    live = Liveness(last_cycle_ms=NOW - 3 * MIN, last_ats_ms=NOW - 20 * MIN, last_success_ms=NOW - 3 * MIN)
    s = status_line(live, ats_minutes=30, now_ms=NOW)
    assert s.state == "running" and s.last_ago == "3m ago" and s.next_eta == "in about 10 min"


# ---- Home page: status, figures, needs-attention ----

def test_home_waiting_state(tmp_path, monkeypatch):
    html = client_for(make_app(tmp_path, monkeypatch)).get("/home").text
    assert "Waiting for the first check" in html


def test_home_running_state_and_figures(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    seed_cycle(app, minutes_ago=3)
    seed_match(app, "a:1")
    html = client_for(app).get("/home").text
    assert "Running" in html and "last checked 3m ago" in html
    assert re.search(r'<dt>New matches</dt><dd><a href="/">1</a></dd>', html)


def test_home_stalled_state_links_system_health(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    seed_cycle(app, minutes_ago=24 * 60)
    html = client_for(app).get("/home").text
    assert "Stalled" in html and 'href="/pipeline">Open System health' in html


def test_status_partial_polls_itself(tmp_path, monkeypatch):
    r = client_for(make_app(tmp_path, monkeypatch)).get("/home/status")
    assert r.status_code == 200
    assert 'id="home-status"' in r.text and 'hx-get="/home/status"' in r.text
    assert 'hx-swap="outerHTML"' in r.text


def test_new_matches_figure_degrades_alone(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)

    def boom():
        raise RuntimeError("locked")
    monkeypatch.setattr(app.state.repo, "status_counts", boom)
    html = client_for(app).get("/home").text
    assert "<dt>New matches</dt><dd>—</dd>" in html
    assert re.search(r"<dt>Boards watched</dt><dd>\d", html)


def test_needs_attention_matches_settings_overview(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    c = client_for(app)
    home = re.search(r'<ul class="readiness">.*?</ul>', c.get("/home").text, re.S).group(0)
    over = re.search(r'<ul class="readiness">.*?</ul>', c.get("/settings/overview").text, re.S).group(0)
    assert home == over and "no posting can ever match" in home


# ---- boards figure respects the starter-pack gate ----

_WD = {"tenant": "acme", "region": "wd5", "site": "Ext"}


def _seed_mixed_rows(app):
    d, b = app.state.stores.discovered, app.state.stores.boards
    d.upsert_ok("lever:found-co", last_posting_count=3)            # discovery's own
    d.seed_ok("greenhouse:stripe", company_name="Stripe", origin="starter")
    b.seed_ok("acme.com", name="Acme", family="workday", identity=_WD,
              connector_name="workday:acme:Ext", company="Acme", origin="starter")


def test_boards_figure_excludes_hidden_starter_rows(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch,
                   settings={"relevance": {"score_high": 7, "score_low": 4},
                             "discovery": {"starter_pack": False}})
    _seed_mixed_rows(app)
    html = client_for(app).get("/home").text
    assert re.search(r"<dt>Boards watched</dt><dd>1\b", html)


def test_boards_figure_counts_starter_rows_when_pack_on(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch,
                   settings={"relevance": {"score_high": 7, "score_low": 4},
                             "discovery": {"starter_pack": True}})
    _seed_mixed_rows(app)
    html = client_for(app).get("/home").text
    assert re.search(r"<dt>Boards watched</dt><dd>3\b", html)
