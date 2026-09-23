from tests.web.shell_helpers import client_for, make_app, seed_cycle, seed_match


def test_matches_before_first_check(tmp_path, monkeypatch):
    html = client_for(make_app(tmp_path, monkeypatch)).get("/jobs").text
    assert "Your first check hasn&#39;t finished" in html
    assert 'href="/home"' in html


def test_matches_checks_ran_nothing_matched(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    seed_cycle(app, minutes_ago=5)
    html = client_for(app).get("/jobs").text
    assert "Nothing has matched yet" in html
    assert 'href="/settings/filters"' in html and 'href="/audit"' in html


def test_matches_hidden_by_filters(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    seed_cycle(app, minutes_ago=5)
    seed_match(app, "a:1")
    html = client_for(app).get("/jobs", params={"q": "zzz-no-such-title"}).text
    assert "Nothing matches these filters" in html
    assert 'onclick="triageResetFilters()"' in html


def test_filtered_is_the_fallback_when_counts_fail(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)

    def boom():
        raise RuntimeError("locked")
    monkeypatch.setattr(app.state.repo, "status_counts", boom)
    r = client_for(app).get("/jobs", params={"q": "zzz"})
    assert r.status_code == 200 and "Nothing matches these filters" in r.text


def test_rows_render_no_empty_state(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    seed_match(app, "a:1")
    assert "empty-state" not in client_for(app).get("/jobs").text


def test_inbox_defines_reset(tmp_path, monkeypatch):
    html = client_for(make_app(tmp_path, monkeypatch)).get("/").text
    assert "function triageResetFilters()" in html


def test_board_empty_state(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    html = client_for(app).get("/board").text
    assert "Applications you&#39;re tracking show up here" in html
    assert 'href="/board?add=1"' in html
    assert 'class="board"' not in html


def test_board_with_a_card_has_no_empty_state(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    seed_match(app, "a:1", status="applied")
    html = client_for(app).get("/board").text
    assert "show up here" not in html and 'class="board"' in html


def test_board_add_param_opens_the_form(tmp_path, monkeypatch):
    c = client_for(make_app(tmp_path, monkeypatch))
    assert '<details class="add-job" open>' in c.get("/board?add=1").text
    assert '<details class="add-job">' in c.get("/board").text


def test_board_error_is_not_reported_as_empty(tmp_path, monkeypatch):
    from src.web.board import Board
    app = make_app(tmp_path, monkeypatch)
    monkeypatch.setattr(app.state.board, "board", lambda: Board(columns={}, archive=[], error=True))
    html = client_for(app).get("/board").text
    assert "temporarily unavailable" in html and "show up here" not in html
