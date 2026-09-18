"""The generic row editor: add, edit, remove one entry of a rows field."""
from src.settings.errors import SettingsInvalid, StaleWrite
from src.web.app import create_app
from tests.auth_helpers import signed_in_client
from tests.settings_helpers import make_service


def _app(tmp_path, monkeypatch, service=None):
    monkeypatch.setenv("JOB_AGG_SQLITE_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("JOB_AGG_TAILORED_DIR", str(tmp_path / "tailored"))
    return create_app(service=service if service is not None else make_service({}))


ONE = {"sources": {"workday": [{"tenant": "microsoft", "region": "wd1", "site": "External"}]}}


def _digest(app, path="sources.workday"):
    from src.settings.rows import list_rows
    return list_rows(app.state.service.snapshot().cfg, path)[0].digest


def _field_block(html: str, name: str) -> str:
    """The markup for one `<div class="field">` block, located by an input's
    `name` attribute — so a test can assert an error is nested under the
    field it belongs to, not merely present somewhere on the page."""
    start = html.index(f'name="{name}"')
    end = html.find('<div class="field', start)
    if end == -1:
        end = html.index("</form>", start)
    return html[start:end]


def test_new_form_renders_one_input_per_item_field(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    r = signed_in_client(app).get("/settings/rows/sources.workday/new")
    assert r.status_code == 200
    for field in ("tenant", "region", "site"):
        assert f'name="item.{field}"' in r.text


def test_new_form_is_a_standalone_page_not_a_partial(tmp_path, monkeypatch):
    """Ruling R7: this is a full page extending settings_base.html — inside
    settings_advanced_group.html's own outer <form>, a nested <form> would be
    silently dropped by the browser and Add/Edit would do nothing."""
    app = _app(tmp_path, monkeypatch)
    r = signed_in_client(app).get("/settings/rows/sources.workday/new")
    assert r.status_code == 200
    assert "<html" in r.text.lower()
    assert 'class="settings"' in r.text  # the shared settings shell/nav


def test_add_writes_the_entry(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    client = signed_in_client(app, follow_redirects=False)
    r = client.post("/settings/rows/sources.workday",
                    data={"item.tenant": "acme", "item.region": "wd3", "item.site": "External"})
    assert r.status_code == 303
    assert r.headers["location"].startswith("/settings/companies")
    tenants = [t.tenant for t in app.state.service.snapshot().cfg.sources.workday]
    assert tenants == ["acme"]


def test_a_successful_add_redirects_to_the_owning_advanced_group(tmp_path, monkeypatch):
    """hiringcafe.extra_queries is not a board — it belongs to the Aggregators
    advanced group, not the Companies page."""
    app = _app(tmp_path, monkeypatch)
    client = signed_in_client(app, follow_redirects=False)
    r = client.post("/settings/rows/sources.hiringcafe.extra_queries",
                    data={"item.query": "staff platform engineer"})
    assert r.status_code == 303
    assert r.headers["location"] == "/settings/advanced/sources?added=1"
    queries = app.state.service.snapshot().cfg.sources.hiringcafe.extra_queries
    assert queries == ["staff platform engineer"]


def test_edit_form_is_prefilled(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, make_service(ONE))
    r = signed_in_client(app).get(f"/settings/rows/sources.workday/{_digest(app)}/edit")
    assert 'value="microsoft"' in r.text


def test_update_replaces_only_that_row(tmp_path, monkeypatch):
    service = make_service({"sources": {"workday": [
        {"tenant": "microsoft", "region": "wd1", "site": "External"},
        {"tenant": "salesforce", "region": "wd12", "site": "External"},
    ]}})
    app = _app(tmp_path, monkeypatch, service)
    r = signed_in_client(app, follow_redirects=False).post(
        f"/settings/rows/sources.workday/{_digest(app)}",
        data={"item.tenant": "microsoft", "item.region": "wd1", "item.site": "Internal"})
    assert r.status_code == 303
    sites = [t.site for t in app.state.service.snapshot().cfg.sources.workday]
    assert sites == ["Internal", "External"]


def test_remove_drops_the_row(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, make_service(ONE))
    r = signed_in_client(app, follow_redirects=False).post(
        f"/settings/rows/sources.workday/{_digest(app)}/remove", data={"confirm": "yes"})
    assert r.status_code == 303
    assert app.state.service.snapshot().cfg.sources.workday == []


def test_remove_confirm_page_names_the_entry_and_posts_to_remove(tmp_path, monkeypatch):
    """Ruling R8: Remove is a GET confirm PAGE, not an inline form — this is
    that page, reached by following the plain <a> _rows.html renders."""
    app = _app(tmp_path, monkeypatch, make_service(ONE))
    digest = _digest(app)
    r = signed_in_client(app).get(f"/settings/rows/sources.workday/{digest}/remove")
    assert r.status_code == 200
    assert "microsoft" in r.text
    assert f'action="/settings/rows/sources.workday/{digest}/remove"' in r.text
    # visiting the confirm page must not itself remove anything
    assert app.state.service.snapshot().cfg.sources.workday[0].tenant == "microsoft"


def test_remove_confirm_for_a_stale_digest_reports_gone(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, make_service(ONE))
    r = signed_in_client(app).get("/settings/rows/sources.workday/000000000000/remove")
    assert r.status_code == 409
    assert "no longer configured" in r.text


def test_remove_confirm_is_gated_the_same_way_as_new(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    r = signed_in_client(app).get("/settings/rows/filters.titles/000000000000/remove")
    assert r.status_code == 404


def test_the_rows_branch_never_nests_a_form_in_the_group_form(tmp_path, monkeypatch):
    """settings_advanced_group.html wraps every field it renders in one outer
    <form action="/settings/advanced/{key}">. Per HTML5 tree construction, a
    second <form> inside it is a parse error and the browser drops it,
    promoting its button into the OUTER form's submit control — so a nested
    Remove form would silently submit "Save Aggregators" instead of removing
    anything. The rows branch must never emit a <form>; Remove is a plain <a>
    to the confirm page above (Ruling R8)."""
    app = _app(tmp_path, monkeypatch, make_service(
        {"sources": {"hiringcafe": {"extra_queries": ["staff platform engineer"]}}}))
    r = signed_in_client(app).get("/settings/advanced/sources")
    assert r.status_code == 200
    start = r.text.index('<form method="post" action="/settings/advanced/sources">')
    end = r.text.index("</form>", start)
    assert "<form" not in r.text[start + 1:end]

    from src.settings.rows import list_rows
    digest = list_rows(app.state.service.snapshot().cfg,
                       "sources.hiringcafe.extra_queries")[0].digest
    assert f'href="/settings/rows/sources.hiringcafe.extra_queries/{digest}/remove"' in r.text


def test_a_stale_write_during_update_preserves_the_submitted_values(tmp_path, monkeypatch):
    """_apply's failure paths must not blank the form out from under a
    submission that already passed validation — this mirrors
    save_section's render_error, which always re-renders with submitted=raw,
    on every one of its failure paths including StaleWrite."""
    app = _app(tmp_path, monkeypatch, make_service(ONE))
    digest = _digest(app)

    def boom(mutate, *, source):
        raise StaleWrite("conflict")

    monkeypatch.setattr(app.state.service, "update_settings", boom)
    r = signed_in_client(app).post(
        f"/settings/rows/sources.workday/{digest}",
        data={"item.tenant": "microsoft", "item.region": "wd1", "item.site": "Internal"})
    assert r.status_code == 200
    assert "saved while you were editing" in r.text
    block = _field_block(r.text, "item.site")
    assert 'value="Internal"' in block
    assert f'action="/settings/rows/sources.workday/{digest}"' in r.text


def test_a_model_level_failure_during_remove_keeps_the_rows_values(tmp_path, monkeypatch):
    """A remove that fails at the whole-document level (a cross-field
    validator, not this row) must still show what it was about to remove,
    not a blank edit form."""
    app = _app(tmp_path, monkeypatch, make_service(ONE))
    digest = _digest(app)

    def boom(mutate, *, source):
        raise SettingsInvalid([{"loc": "", "msg": "cross-field problem"}])

    monkeypatch.setattr(app.state.service, "update_settings", boom)
    r = signed_in_client(app).post(f"/settings/rows/sources.workday/{digest}/remove", data={})
    assert r.status_code == 200
    assert "cross-field problem" in r.text
    block = _field_block(r.text, "item.tenant")
    assert 'value="microsoft"' in block


def test_a_stale_digest_reports_instead_of_touching_a_neighbour(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, make_service(ONE))
    r = signed_in_client(app).post(
        "/settings/rows/sources.workday/000000000000",
        data={"item.tenant": "evil", "item.region": "wd1", "item.site": "External"})
    assert r.status_code == 409
    assert "no longer configured" in r.text
    assert app.state.service.snapshot().cfg.sources.workday[0].tenant == "microsoft"


def test_an_invalid_value_renders_inline_and_writes_nothing(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    r = signed_in_client(app).post(
        "/settings/rows/sources.eightfold",
        data={"item.slug": "ngc", "item.domain": "ngc.com", "item.flavor": "nonsense"})
    assert r.status_code == 200
    block = _field_block(r.text, "item.flavor")
    assert "field-error" in block            # placed under the field...
    assert "pcsx" in block                   # ...with pydantic's allowed values...
    assert '<p class="bad">' not in r.text   # ...not dumped in the form-level banner
    assert app.state.service.snapshot().cfg.sources.eightfold == []


def test_a_whitespace_only_required_field_is_rejected(tmp_path, monkeypatch):
    """_value() (src/web/settings/forms.py) strips a text value before this
    layer ever sees it, so a query of all spaces arrives as "". None of the
    row element models enforce non-empty on a plain str field (only rows.py's
    own scalar/chips case does), so without a guard here pydantic would accept
    it and silently write a blank search."""
    app = _app(tmp_path, monkeypatch)
    r = signed_in_client(app).post(
        "/settings/rows/sources.hiringcafe.extra_queries", data={"item.query": "   "})
    assert r.status_code == 200
    block = _field_block(r.text, "item.query")
    assert "field-error" in block
    assert app.state.service.snapshot().cfg.sources.hiringcafe.extra_queries == []


def test_a_slug_family_row_can_be_removed(tmp_path, monkeypatch):
    """The Companies page lists Greenhouse slugs beside Workday tenants and
    must remove either through this one route."""
    app = _app(tmp_path, monkeypatch, make_service({"sources": {"greenhouse": ["acme", "beta"]}}))
    r = signed_in_client(app, follow_redirects=False).post(
        f"/settings/rows/sources.greenhouse/{_digest(app, 'sources.greenhouse')}/remove",
        data={"confirm": "yes"})
    assert r.status_code == 303
    assert app.state.service.snapshot().cfg.sources.greenhouse == ["beta"]


def test_a_chips_field_outside_sources_is_not_editable_here(tmp_path, monkeypatch):
    """filters.titles has its own input on its own page; a second editing
    path for it would be a second way to get it wrong."""
    app = _app(tmp_path, monkeypatch)
    assert signed_in_client(app).get("/settings/rows/filters.titles/new").status_code == 404


def test_a_path_that_is_not_a_list_is_rejected(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    r = signed_in_client(app).get("/settings/rows/relevance.score_low/new")
    assert r.status_code == 404


def test_an_unknown_path_is_rejected(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    r = signed_in_client(app).get("/settings/rows/nope.nothing/new")
    assert r.status_code == 404


def test_add_is_rejected_the_same_way_as_new(tmp_path, monkeypatch):
    """POST must be gated the same as GET — an editable-paths bypass on one
    verb but not the other would be a hole, not a feature."""
    app = _app(tmp_path, monkeypatch)
    r = signed_in_client(app).post("/settings/rows/filters.titles", data={"item.value": "x"})
    assert r.status_code == 404
