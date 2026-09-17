"""Cross-origin protection for state-changing requests (the algorithm of Go
1.25's http.CrossOriginProtection)."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.web.cross_origin import cross_origin_allowed, register_cross_origin_guard


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
def test_safe_methods_are_never_checked(method):
    headers = {"sec-fetch-site": "cross-site", "origin": "http://evil.example", "host": "box:8000"}
    assert cross_origin_allowed(method, headers) is True


@pytest.mark.parametrize("site, allowed", [
    ("same-origin", True), ("none", True), ("same-site", False), ("cross-site", False),
])
def test_sec_fetch_site_decides_when_present(site, allowed):
    # Origin matches Host here; Sec-Fetch-Site still decides.
    headers = {"sec-fetch-site": site, "origin": "http://box:8000", "host": "box:8000"}
    assert cross_origin_allowed("POST", headers) is allowed


@pytest.mark.parametrize("origin, allowed", [
    ("http://box:8000", True),
    ("http://BOX:8000", True),
    ("https://box:8000", True),     # only the host is compared, as in Go
    ("http://box:8001", False),     # another service on the same host
    ("http://box", False),
    ("http://evil.example", False),
    ("null", False),
    ("", False),
])
def test_origin_must_match_host_without_sec_fetch_site(origin, allowed):
    assert cross_origin_allowed("POST", {"origin": origin, "host": "box:8000"}) is allowed


def test_requests_with_neither_header_are_not_from_a_browser():
    assert cross_origin_allowed("POST", {"host": "box:8000"}) is True


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "post"])
def test_every_unsafe_method_is_checked(method):
    assert cross_origin_allowed(method, {"origin": "http://evil.example", "host": "box:8000"}) is False


def _client() -> TestClient:
    app = FastAPI()
    register_cross_origin_guard(app)

    @app.post("/thing")
    def write_thing():
        return {"ok": True}

    @app.get("/thing")
    def read_thing():
        return {"ok": True}

    return TestClient(app)


def test_guard_blocks_a_cross_origin_post_and_logs(caplog):
    with caplog.at_level("WARNING", logger="src.web.cross_origin"):
        r = _client().post("/thing", headers={"Origin": "http://evil.example"})
    assert r.status_code == 403
    assert r.text == "Cross-origin request blocked."
    [record] = [x for x in caplog.records if x.message == "cross_origin_blocked"]
    assert (record.method, record.path) == ("POST", "/thing")
    assert (record.origin, record.host, record.sec_fetch_site) == (
        "http://evil.example", "testserver", None)


def test_guard_passes_same_origin_and_non_browser_requests():
    c = _client()
    assert c.post("/thing", headers={"Origin": "http://testserver"}).status_code == 200
    assert c.post("/thing", headers={"Sec-Fetch-Site": "same-origin"}).status_code == 200
    assert c.post("/thing").status_code == 200
    assert c.get("/thing", headers={"Origin": "http://evil.example"}).status_code == 200


def test_the_app_installs_the_guard():
    from src.sqlite_db import connect
    from src.web.app import create_app
    from tests.settings_helpers import configured_stores

    client = TestClient(create_app(stores=configured_stores(connect(":memory:"))))
    r = client.post("/status", params={"id": "x", "status": "applied"},
                    headers={"Origin": "http://evil.example"})
    assert r.status_code == 403
