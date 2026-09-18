"""History: see every version, see what one would change, go back to it."""
from src.web.app import create_app
from tests.auth_helpers import signed_in_client
from tests.settings_helpers import make_service


def _app(tmp_path, monkeypatch, service):
    monkeypatch.setenv("JOB_AGG_SQLITE_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("JOB_AGG_TAILORED_DIR", str(tmp_path / "tailored"))
    return create_app(service=service)


def _three(tmp_path, monkeypatch):
    service = make_service({"relevance": {"score_low": 4}})
    service.save_settings({"relevance": {"score_low": 5}}, source="ui", note="ui: LLM")
    service.save_settings({"relevance": {"score_low": 6}}, source="ui", note="ui: LLM again")
    return _app(tmp_path, monkeypatch, service)


def test_the_list_shows_every_version_newest_first(tmp_path, monkeypatch):
    app = _three(tmp_path, monkeypatch)
    r = signed_in_client(app).get("/settings/history")
    assert r.status_code == 200
    assert "ui: LLM again" in r.text and "test fixture" in r.text
    assert r.text.index("ui: LLM again") < r.text.index("test fixture")


def test_the_version_in_effect_is_marked(tmp_path, monkeypatch):
    app = _three(tmp_path, monkeypatch)
    in_effect = app.state.service.snapshot().version_id
    r = signed_in_client(app).get("/settings/history")
    assert f'data-version="{in_effect}"' in r.text
    assert "in effect" in r.text


def test_a_version_page_shows_what_it_would_change(tmp_path, monkeypatch):
    app = _three(tmp_path, monkeypatch)
    oldest = app.state.service.versions(limit=None)[-1].id
    r = signed_in_client(app).get(f"/settings/history/{oldest}")
    assert "relevance.score_low" in r.text
    assert "Restore" in r.text
    # Direction matters: the version in effect (score_low=6) is what running
    # right now, so it is `old`; the version being viewed (score_low=4) is
    # `new`, since restoring it is what would apply that value. Reversing
    # the two arguments renders a plausible-looking but exactly inverted
    # page with no type error to catch it.
    assert "6 -&gt; 4" in r.text  # Jinja autoescapes "->" in the rendered diff line


def test_the_version_in_effect_shows_no_changes(tmp_path, monkeypatch):
    app = _three(tmp_path, monkeypatch)
    current = app.state.service.snapshot().version_id
    r = signed_in_client(app).get(f"/settings/history/{current}")
    assert "no differences" in r.text.lower()


def test_restoring_writes_a_new_version_and_applies(tmp_path, monkeypatch):
    app = _three(tmp_path, monkeypatch)
    oldest = app.state.service.versions(limit=None)[-1].id
    before = len(app.state.service.versions(limit=None))
    r = signed_in_client(app, follow_redirects=False).post(f"/settings/history/{oldest}/restore")
    assert r.status_code == 303
    assert app.state.service.snapshot().cfg.relevance.score_low == 4
    assert len(app.state.service.versions(limit=None)) == before + 1


def test_an_unknown_version_is_a_404(tmp_path, monkeypatch):
    app = _three(tmp_path, monkeypatch)
    assert signed_in_client(app).get("/settings/history/9999").status_code == 404
    assert signed_in_client(app).post("/settings/history/9999/restore").status_code == 404


def test_a_version_that_no_longer_validates_explains_and_offers_no_restore(tmp_path, monkeypatch):
    service = make_service({"relevance": {"score_low": 4}})
    # Write a row the model rejects, straight past the service's validation.
    service._store.insert_settings(          # noqa: SLF001 - deliberate corruption
        doc={"relevance": {"score_low": 99}}, source="ui", note="bad",
        schema_version=1)
    app = _app(tmp_path, monkeypatch, service)
    bad = app.state.service.versions(limit=None)[0].id
    r = signed_in_client(app).get(f"/settings/history/{bad}")
    assert r.status_code == 200
    assert "cannot be restored" in r.text.lower()
    assert f"/settings/history/{bad}/restore" not in r.text
    assert signed_in_client(app).post(f"/settings/history/{bad}/restore").status_code == 409


def test_the_page_says_a_restore_will_make_the_next_file_import_refuse(tmp_path, monkeypatch):
    app = _three(tmp_path, monkeypatch)
    r = signed_in_client(app).get("/settings/history")
    assert "export" in r.text.lower()


def test_restoring_shows_the_saved_banner_after_the_redirect(tmp_path, monkeypatch):
    """Important 4 (whole-branch review): history_restore redirects to
    /settings/history?restored=1, but nothing used to read that flag —
    settings_base.html's "Saved" banner never fired for a restore, the one
    write on this page that uses a redirect rather than a direct
    POST-then-render (history_restore is a plain @app.post with no
    response_class="text/html" render of its own, unlike save_section's
    callers)."""
    app = _three(tmp_path, monkeypatch)
    oldest = app.state.service.versions(limit=None)[-1].id
    client = signed_in_client(app, follow_redirects=False)
    r = client.post(f"/settings/history/{oldest}/restore")
    assert r.headers["location"] == "/settings/history?restored=1"
    landing = client.get(r.headers["location"])
    assert "Saved — running live" in landing.text


def test_the_htmx_lazy_diff_selector_matches_the_diff_partials_id(tmp_path, monkeypatch):
    """Whole-branch review: the entire History lazy-diff feature rests on a
    bare string contract between settings_history.html's
    hx-select="#history-diff" and _history_diff.html's id="history-diff" —
    nothing in the type system or the rest of the suite ties them together,
    so renaming either half breaks the feature in the browser (htmx finds
    nothing to select, so hx-target never gets filled) while every other
    test in this file stays green. Rendering both real pages is a stand-in
    for that comparison: htmx itself does the actual selecting client-side,
    so the most this can assert from outside a browser is that the two
    identifiers still appear, spelled identically, in the two places htmx
    is told to look."""
    app = _three(tmp_path, monkeypatch)
    version_id = app.state.service.snapshot().version_id
    list_page = signed_in_client(app).get("/settings/history")
    assert 'hx-select="#history-diff"' in list_page.text
    diff_page = signed_in_client(app).get(f"/settings/history/{version_id}")
    assert 'id="history-diff"' in diff_page.text
