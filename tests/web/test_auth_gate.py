"""The login gate: which paths need a session, and how requests without one
are answered."""
import sqlite3
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from src.auth.service import AuthService
from src.settings.service import ConfigService
from src.sqlite_db import connect
from src.web.app import create_app
from src.web.auth import SESSION_COOKIE
from tests.auth_helpers import FakeClock, cheap_hasher
from tests.settings_helpers import configured_stores
from tests.sqlite_helpers import sqlite_stores

PW = "correct horse"


def _app(*, configured=True, clock=None, password=True):
    conn = connect(":memory:")
    stores = configured_stores(conn) if configured else sqlite_stores(conn)
    auth = AuthService(stores.auth, hasher=cheap_hasher(), clock=clock or FakeClock())
    if password:
        auth.set_password(PW)
    app = create_app(stores=stores, service=ConfigService(stores.settings, env={}), auth=auth)
    return app, auth


def _anon(app) -> TestClient:
    return TestClient(app, follow_redirects=False)


def _signed_in(app) -> TestClient:
    client = _anon(app)
    assert client.post("/login", data={"password": PW}).status_code == 303
    return client


def _path(location: str) -> str:
    return urlsplit(location).path


def _next(location: str) -> str | None:
    return parse_qs(urlsplit(location).query).get("next", [None])[0]


def test_public_paths_need_no_session():
    app, _ = _app()
    client = _anon(app)
    assert client.get("/static/app.css").status_code == 200
    assert client.get("/login").status_code == 200
    assert client.get("/welcome").headers["location"] == "/login?claimed=1"
    r = client.get("/tailor", params={"job_id": "j1", "t": "bad"})
    assert r.status_code == 200 and "expired or is invalid" in r.text
    assert client.get("/tailor/pdf", params={"job_id": "j1", "t": "bad"}).status_code == 403
    assert client.post("/logout").headers["location"] == "/login"


@pytest.mark.parametrize("path", ["/tailor/history", "/tailored/j1.pdf", "/loginx", "/staticx", "/static"])
def test_public_matching_is_exact(path):
    app, _ = _app()
    r = _anon(app).get(path)
    assert r.status_code == 303 and _path(r.headers["location"]) == "/login"


def test_without_a_password_everything_goes_to_welcome():
    app, _ = _app(password=False)
    client = _anon(app)
    r = client.get("/board")
    assert r.status_code == 303 and r.headers["location"] == "/welcome"
    r = client.get("/jobs/new-count", headers={"HX-Request": "true"})
    assert r.status_code == 401 and r.headers["hx-redirect"] == "/welcome"
    assert client.post("/status", params={"id": "x", "status": "applied"}).status_code == 401
    assert client.get("/welcome").status_code == 200


def test_without_a_session_pages_redirect_to_login_with_next():
    app, _ = _app()
    client = _anon(app)
    r = client.get("/board", params={"x": "1"})
    assert r.status_code == 303
    assert _path(r.headers["location"]) == "/login" and _next(r.headers["location"]) == "/board?x=1"
    assert client.get("/").headers["location"] == "/login"
    assert client.head("/board").status_code == 303


def test_htmx_requests_get_401_and_return_to_the_issuing_page():
    app, _ = _app()
    r = _anon(app).get("/pipeline/cycles", headers={
        "HX-Request": "true", "HX-Current-URL": "http://testserver/pipeline?tab=2"})
    assert r.status_code == 401
    assert _path(r.headers["hx-redirect"]) == "/login"
    assert _next(r.headers["hx-redirect"]) == "/pipeline?tab=2"


def test_other_methods_get_401():
    app, _ = _app()
    r = _anon(app).post("/status", params={"id": "x", "status": "applied"})
    assert r.status_code == 401 and r.text == "Sign in required."


def test_an_unknown_cookie_is_cleared():
    app, _ = _app()
    client = _anon(app)
    client.cookies.set(SESSION_COOKIE, "bogus")
    r = client.get("/board")
    assert r.status_code == 303
    assert any(h.startswith(f"{SESSION_COOKIE}=") and "Max-Age=0" in h
               for h in r.headers.get_list("set-cookie"))


def test_signed_in_pages_render_with_the_account_header():
    app, _ = _app()
    r = _signed_in(app).get("/")
    assert r.status_code == 200
    assert 'action="/logout"' in r.text and 'href="/account/password"' in r.text


def test_idle_sessions_expire():
    clock = FakeClock()
    app, _ = _app(clock=clock)
    client = _signed_in(app)
    clock.advance(days=30)
    r = client.get("/board")
    assert r.status_code == 303 and _path(r.headers["location"]) == "/login"


def test_the_cookie_is_reissued_only_when_the_session_is_touched():
    clock = FakeClock()
    app, _ = _app(clock=clock)
    client = _signed_in(app)
    token = client.cookies[SESSION_COOKIE]
    clock.advance(minutes=30)
    assert client.get("/").headers.get_list("set-cookie") == []
    clock.advance(minutes=31)
    [cookie] = client.get("/").headers.get_list("set-cookie")
    assert cookie.startswith(f"{SESSION_COOKIE}={token};") and "Max-Age=2592000" in cookie


def test_a_cookie_set_by_the_route_is_not_overwritten_by_the_touch():
    clock = FakeClock()
    app, auth = _app(clock=clock)
    client = _signed_in(app)
    old = client.cookies[SESSION_COOKIE]
    clock.advance(hours=2)  # this request touches the old session
    r = client.post("/account/password", data={
        "current": PW, "password": "new password 1", "confirm": "new password 1"})
    cookies = [h for h in r.headers.get_list("set-cookie") if h.startswith(f"{SESSION_COOKIE}=")]
    assert len(cookies) == 1 and not cookies[0].startswith(f"{SESSION_COOKIE}={old};")
    assert auth.resolve(client.cookies[SESSION_COOKIE]) is not None


def test_signed_in_before_setup_lands_on_setup():
    app, _ = _app(configured=False)
    client = _signed_in(app)
    r = client.get("/")
    assert r.status_code == 303 and r.headers["location"] == "/setup"
    assert client.get("/setup").status_code == 200
    assert client.get("/account/password").status_code == 200


def test_a_password_reset_elsewhere_signs_everyone_out():
    app, auth = _app()
    client = _signed_in(app)
    auth.set_password("reset password")  # e.g. the CLI in another container
    r = client.get("/board")
    assert r.status_code == 303 and _path(r.headers["location"]) == "/login"


class _BrokenAuth:
    def has_password(self):
        raise sqlite3.OperationalError("disk I/O error")


def test_an_unreadable_login_store_fails_closed(caplog):
    stores = configured_stores(connect(":memory:"))
    client = TestClient(create_app(stores=stores, auth=_BrokenAuth()), follow_redirects=False)
    client.cookies.set(SESSION_COOKIE, "anything")
    with caplog.at_level("ERROR", logger="src.web.auth"):
        r = client.get("/board")
    assert r.status_code == 503 and r.text == "Cannot read the login database."
    assert "auth_store_unavailable" in [x.message for x in caplog.records]
    assert client.get("/static/app.css").status_code == 200
