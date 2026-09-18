"""The union list: hiring.cafe extra queries through the generic row editor."""
from src.web.app import create_app
from tests.auth_helpers import signed_in_client
from tests.settings_helpers import make_service

PATH = "sources.hiringcafe.extra_queries"


def _app(tmp_path, monkeypatch, service=None):
    monkeypatch.setenv("JOB_AGG_SQLITE_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("JOB_AGG_TAILORED_DIR", str(tmp_path / "tailored"))
    return create_app(service=service if service is not None else make_service({}))


def _digest(app, index=0):
    from src.settings.rows import list_rows
    return list_rows(app.state.service.snapshot().cfg, PATH)[index].digest


def test_the_aggregators_page_lists_the_queries(tmp_path, monkeypatch):
    service = make_service({"sources": {"hiringcafe": {"extra_queries": [
        "rust", {"query": "golang", "location": "DE"}]}}})
    r = signed_in_client(_app(tmp_path, monkeypatch, service)).get("/settings/advanced/sources")
    assert r.status_code == 200
    assert "rust" in r.text and "golang" in r.text
    assert "DE" in r.text


def test_adding_a_plain_query_stores_a_bare_string(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    signed_in_client(app).post(f"/settings/rows/{PATH}",
                               data={"item.query": "zig", "item.location": ""})
    _, doc = app.state.service.current_doc()
    assert doc["sources"]["hiringcafe"]["extra_queries"] == ["zig"]


def test_adding_a_geo_query_stores_a_mapping(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    signed_in_client(app).post(f"/settings/rows/{PATH}",
                               data={"item.query": "zig", "item.location": "DE"})
    _, doc = app.state.service.current_doc()
    assert doc["sources"]["hiringcafe"]["extra_queries"] == [{"query": "zig", "location": "DE"}]


def test_editing_a_bare_string_into_a_geo_query_works(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch,
               make_service({"sources": {"hiringcafe": {"extra_queries": ["rust"]}}}))
    signed_in_client(app).post(f"/settings/rows/{PATH}/{_digest(app)}",
                               data={"item.query": "rust", "item.location": "FR"})
    queries = app.state.service.snapshot().cfg.sources.hiringcafe.extra_queries
    assert queries[0].query == "rust" and queries[0].location == "FR"


def test_an_invalid_location_is_an_inline_error(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    r = signed_in_client(app).post(
        f"/settings/rows/{PATH}",
        data={"item.query": "zig", "item.location": "not-a-country"})
    assert r.status_code == 200
    assert "field-error" in r.text     # placed under the input, not banner-dumped
    assert app.state.service.snapshot().cfg.sources.hiringcafe.extra_queries == []


def test_removing_a_query(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch,
               make_service({"sources": {"hiringcafe": {"extra_queries": ["rust", "go"]}}}))
    signed_in_client(app).post(f"/settings/rows/{PATH}/{_digest(app)}/remove",
                               data={"confirm": "yes"})
    assert app.state.service.snapshot().cfg.sources.hiringcafe.extra_queries == ["go"]
