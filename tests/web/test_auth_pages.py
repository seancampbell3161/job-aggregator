"""The /welcome, /login, /logout, and /account/password pages."""
import pytest
from fastapi.testclient import TestClient

from src.auth.errors import AlreadyClaimed
from src.auth.service import AuthService
from src.auth.throttle import LoginThrottle
from src.settings.service import ConfigService
from src.sqlite_db import connect
from src.web.app import create_app
from src.web.auth import SESSION_COOKIE, safe_next
from tests.auth_helpers import FakeClock, cheap_hasher
from tests.settings_helpers import configured_stores
from tests.sqlite_helpers import sqlite_stores

PW = "correct horse"


class Tick:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def _client(*, base_url="http://testserver", configured=True):
    conn = connect(":memory:")
    stores = configured_stores(conn) if configured else sqlite_stores(conn)
    auth = AuthService(stores.auth, hasher=cheap_hasher(), clock=FakeClock())
    app = create_app(stores=stores, service=ConfigService(stores.settings, env={}), auth=auth)
    return TestClient(app, base_url=base_url, follow_redirects=False), auth


def _session_cookies(response) -> list[str]:
    return [h for h in response.headers.get_list("set-cookie")
            if h.startswith(f"{SESSION_COOKIE}=")]


def _attrs(set_cookie: str) -> dict[str, str]:
    _, *parts = [p.strip() for p in set_cookie.split(";")]
    attrs = {}
    for part in parts:
        name, _, value = part.partition("=")
        attrs[name.lower()] = value
    return attrs


def test_create_app_builds_auth_from_the_stores():
    app = create_app(stores=configured_stores(connect(":memory:")))
    assert isinstance(app.state.auth, AuthService)
    assert isinstance(app.state.login_throttle, LoginThrottle)


def test_welcome_form_shows_until_a_password_exists():
    client, auth = _client()
    r = client.get("/welcome")
    assert r.status_code == 200
    assert 'action="/welcome"' in r.text and 'autocomplete="new-password"' in r.text
    auth.set_password(PW)
    r = client.get("/welcome")
    assert r.status_code == 303 and r.headers["location"] == "/login?claimed=1"


def test_welcome_creates_the_password_and_signs_in():
    client, auth = _client()
    r = client.post("/welcome", data={"password": PW, "confirm": PW})
    assert r.status_code == 303 and r.headers["location"] == "/"
    [cookie] = _session_cookies(r)
    attrs = _attrs(cookie)
    assert "httponly" in attrs and attrs["samesite"].lower() == "lax"
    assert attrs["path"] == "/" and attrs["max-age"] == "2592000"
    assert "secure" not in attrs
    assert auth.resolve(client.cookies[SESSION_COOKIE]) is not None
    assert auth.login(PW) is not None


def test_the_session_cookie_is_secure_over_https():
    client, _ = _client(base_url="https://testserver")
    r = client.post("/welcome", data={"password": PW, "confirm": PW})
    [cookie] = _session_cookies(r)
    assert "secure" in _attrs(cookie)


@pytest.mark.parametrize("password, confirm, message", [
    (PW, "different horse", "t match."),
    ("short", "short", "Use at least 8 characters."),
])
def test_welcome_rejects_bad_input_and_stores_nothing(password, confirm, message):
    client, auth = _client()
    r = client.post("/welcome", data={"password": password, "confirm": confirm})
    assert r.status_code == 400
    assert message in r.text
    assert auth.has_password() is False
    assert _session_cookies(r) == []


def test_welcome_after_a_claim_keeps_the_first_password():
    client, auth = _client()
    auth.claim(PW)
    r = client.post("/welcome", data={"password": "attacker pass", "confirm": "attacker pass"})
    assert r.status_code == 303 and r.headers["location"] == "/login?claimed=1"
    assert auth.login(PW) is not None and auth.login("attacker pass") is None


def test_losing_a_claim_race_redirects_to_login(monkeypatch):
    client, auth = _client()

    def lost_race(password):
        raise AlreadyClaimed("claimed")

    monkeypatch.setattr(auth, "claim", lost_race)
    r = client.post("/welcome", data={"password": PW, "confirm": PW})
    assert r.status_code == 303 and r.headers["location"] == "/login?claimed=1"


def test_login_page():
    client, auth = _client()
    assert client.get("/login").headers["location"] == "/welcome"
    auth.set_password(PW)
    r = client.get("/login", params={"claimed": "1", "next": "/board"})
    assert r.status_code == 200
    assert "already has a password" in r.text
    assert 'autocomplete="username"' in r.text and 'autocomplete="current-password"' in r.text
    assert 'name="next" value="/board"' in r.text
    assert 'action="/logout"' not in r.text
    r = client.get("/login", params={"next": "//evil.example"})
    assert 'name="next" value="/"' in r.text


def test_login_signs_in_and_follows_only_a_safe_next():
    client, auth = _client()
    auth.set_password(PW)
    r = client.post("/login", data={"password": PW, "next": "/board?x=1"})
    assert r.status_code == 303 and r.headers["location"] == "/board?x=1"
    assert auth.resolve(client.cookies[SESSION_COOKIE]) is not None
    r = client.post("/login", data={"password": PW, "next": "https://evil.example"})
    assert r.headers["location"] == "/"


def test_a_wrong_password_is_401_with_a_generic_message(caplog):
    client, auth = _client()
    auth.set_password(PW)
    with caplog.at_level("WARNING", logger="src.web.auth"):
        r = client.post("/login", data={"password": "wrong horse"})
    assert r.status_code == 401
    assert "Incorrect password." in r.text
    assert _session_cookies(r) == []
    [record] = [x for x in caplog.records if x.message == "login_failed"]
    assert record.consecutive_failures == 1
    assert "wrong horse" not in caplog.text


def test_repeated_failures_are_throttled_without_checking_the_password():
    client, auth = _client()
    auth.set_password(PW)
    clock = Tick()
    client.app.state.login_throttle = LoginThrottle(clock=clock)
    for _ in range(6):
        assert client.post("/login", data={"password": "wrong horse"}).status_code == 401
    r = client.post("/login", data={"password": PW})  # correct, but refused
    assert r.status_code == 429
    assert r.headers["retry-after"] == "2"
    assert "Try again in 2 s." in r.text
    assert _session_cookies(r) == []
    clock.t += 2
    assert client.post("/login", data={"password": PW}).status_code == 303
    assert client.app.state.login_throttle.check() is None


def test_logout_ends_the_session_and_clears_the_cookie():
    client, auth = _client()
    auth.set_password(PW)
    client.post("/login", data={"password": PW})
    token = client.cookies[SESSION_COOKIE]
    r = client.post("/logout")
    assert r.status_code == 303 and r.headers["location"] == "/login"
    [cookie] = _session_cookies(r)
    assert _attrs(cookie)["max-age"] == "0"
    assert auth.resolve(token) is None


def test_change_password():
    client, auth = _client()
    auth.set_password(PW)
    client.post("/login", data={"password": PW})
    old = client.cookies[SESSION_COOKIE]
    other_device = auth.login(PW)
    assert client.get("/account/password").status_code == 200

    def change(current, password, confirm):
        return client.post("/account/password",
                           data={"current": current, "password": password, "confirm": confirm})

    r = change("wrong horse", "new password 1", "new password 1")
    assert r.status_code == 400 and "Incorrect password." in r.text
    r = change(PW, "new password 1", "new password 2")
    assert r.status_code == 400 and "t match." in r.text
    r = change(PW, "short", "short")
    assert r.status_code == 400 and "Use at least 8 characters." in r.text
    assert auth.resolve(old) is not None

    r = change(PW, "new password 1", "new password 1")
    assert r.status_code == 200 and "Password changed" in r.text
    new = client.cookies[SESSION_COOKIE]
    assert new != old
    assert auth.resolve(new) is not None
    assert auth.resolve(old) is None and auth.resolve(other_device) is None
    assert auth.login("new password 1") is not None


def test_cross_origin_login_posts_are_blocked():
    client, auth = _client()
    auth.set_password(PW)
    r = client.post("/login", data={"password": PW}, headers={"Origin": "http://evil.example"})
    assert r.status_code == 403
    assert _session_cookies(r) == []


def test_auth_pages_work_before_setup():
    client, auth = _client(configured=False)
    assert client.get("/welcome").status_code == 200
    auth.set_password(PW)
    assert client.get("/login").status_code == 200
    assert client.post("/login", data={"password": PW}).status_code == 303
    assert client.get("/account/password").status_code == 200
    assert client.post("/logout").status_code == 303


@pytest.mark.parametrize("value, expected", [
    ("/board", "/board"),
    ("/board?x=1&y=2", "/board?x=1&y=2"),
    ("/", "/"),
    ("", "/"),
    ("board", "/"),
    ("//evil.example", "/"),
    ("/\\evil.example", "/"),
    ("https://evil.example", "/"),
    ("javascript:alert(1)", "/"),
    ("/ok\r\nSet-Cookie: x=1", "/"),
])
def test_safe_next(value, expected):
    assert safe_next(value) == expected
