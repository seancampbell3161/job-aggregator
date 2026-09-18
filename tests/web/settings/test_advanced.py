"""The generated pages: one per top-level config key with leftovers."""
from src.web.app import create_app
from tests.auth_helpers import signed_in_client
from tests.settings_helpers import WEB_TEST_SETTINGS, make_service


def _app(tmp_path, monkeypatch, service=None):
    monkeypatch.setenv("JOB_AGG_SQLITE_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("JOB_AGG_TAILORED_DIR", str(tmp_path / "tailored"))
    return create_app(service=service if service is not None else make_service(WEB_TEST_SETTINGS))


def test_index_lists_every_group(tmp_path, monkeypatch):
    from src.web.settings.sections import advanced_groups
    r = signed_in_client(_app(tmp_path, monkeypatch)).get("/settings/advanced")
    assert r.status_code == 200
    for g in advanced_groups():
        assert f'href="/settings/advanced/{g.key}"' in r.text


def test_group_page_renders_typed_inputs(tmp_path, monkeypatch):
    r = signed_in_client(_app(tmp_path, monkeypatch)).get("/settings/advanced/discovery")
    assert r.status_code == 200
    assert 'type="checkbox" name="discovery.enabled"' in r.text
    assert 'name="discovery.yc_oss_min_team_size"' in r.text
    assert 'min="0"' in r.text


def test_group_page_shows_help_from_config_md(tmp_path, monkeypatch):
    r = signed_in_client(_app(tmp_path, monkeypatch)).get("/settings/advanced/discovery")
    assert "team size" in r.text.lower()


def test_saving_a_group_writes_only_that_subtree(tmp_path, monkeypatch):
    service = make_service({**WEB_TEST_SETTINGS, "audit": {"retention_days": 90}})
    app = _app(tmp_path, monkeypatch, service)
    r = signed_in_client(app).post("/settings/advanced/audit", data={
        "audit.enabled": "on", "audit.retention_days": "30"})
    assert r.status_code == 200
    cfg = app.state.service.snapshot().cfg
    assert cfg.audit.retention_days == 30
    assert cfg.relevance.score_high == 7  # untouched


def test_slug_source_families_are_editable(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    r = signed_in_client(app).get("/settings/advanced/sources")
    assert 'name="sources.greenhouse"' in r.text


def test_saving_sources_does_not_wipe_structured_families(tmp_path, monkeypatch):
    service = make_service({**WEB_TEST_SETTINGS, "sources": {"workday": [
        {"tenant": "acme", "region": "wd1", "site": "External"}]}})
    app = _app(tmp_path, monkeypatch, service)
    signed_in_client(app).post("/settings/advanced/sources",
                               data={"sources.greenhouse": ["stripe"]})
    cfg = app.state.service.snapshot().cfg
    assert cfg.sources.greenhouse == ["stripe"]
    assert len(cfg.sources.workday) == 1  # rows fields are never patched by a section form


def test_unknown_group_is_404(tmp_path, monkeypatch):
    c = signed_in_client(_app(tmp_path, monkeypatch))
    assert c.get("/settings/advanced/nope").status_code == 404
    assert c.post("/settings/advanced/nope", data={}).status_code == 404


def test_advanced_is_not_swallowed_by_the_slug_route(tmp_path, monkeypatch):
    """Route order regression guard: /settings/{slug} must not match /settings/advanced."""
    r = signed_in_client(_app(tmp_path, monkeypatch)).get("/settings/advanced")
    assert "Advanced" in r.text
    assert 'href="/settings/advanced/discovery"' in r.text
