"""AuthService: claim, login, sessions (idle expiry, hourly touch), password changes."""
import logging
import sqlite3

import pytest
from argon2 import PasswordHasher

from src.auth.errors import AlreadyClaimed, PasswordRejected, WrongPassword
from src.auth.service import (
    SESSION_IDLE_TTL,
    SESSION_TOUCH_INTERVAL,
    AuthService,
    hash_token,
)
from src.auth.store import SqliteAuthStore
from src.sqlite_db import connect
from tests.auth_helpers import FakeClock, cheap_hasher

PW = "correct horse"


def _service(*, clock=None, hasher=None, conn=None) -> AuthService:
    conn = conn if conn is not None else connect(":memory:")
    return AuthService(SqliteAuthStore(conn), hasher=hasher or cheap_hasher(),
                       clock=clock or FakeClock())


def test_constants():
    assert SESSION_IDLE_TTL.days == 30
    assert SESSION_TOUCH_INTERVAL.total_seconds() == 3600


def test_claim_once():
    auth = _service()
    assert auth.has_password() is False
    token = auth.claim(PW)
    assert auth.has_password() is True
    assert auth.resolve(token) is not None
    with pytest.raises(AlreadyClaimed):
        auth.claim("another password")
    assert auth.login(PW) is not None
    assert auth.login("another password") is None


def test_claim_rejects_a_short_password_and_stores_nothing():
    auth = _service()
    with pytest.raises(PasswordRejected):
        auth.claim("short")
    assert auth.has_password() is False


def test_login_returns_a_fresh_token_and_stores_only_its_hash():
    conn = connect(":memory:")
    auth = _service(conn=conn)
    auth.claim(PW)
    first, second = auth.login(PW), auth.login(PW)
    assert first != second
    stored = {r["token_hash"] for r in conn.execute("SELECT token_hash FROM auth_sessions")}
    assert {hash_token(first), hash_token(second)} <= stored
    dump = "\n".join(conn.iterdump())
    assert first not in dump and second not in dump and PW not in dump


def test_login_without_a_password_or_with_a_wrong_one_is_none():
    auth = _service()
    assert auth.login(PW) is None
    auth.claim(PW)
    assert auth.login("wrong horse") is None


def test_resolve_rejects_missing_and_unknown_tokens():
    auth = _service()
    auth.claim(PW)
    assert auth.resolve(None) is None
    assert auth.resolve("") is None
    assert auth.resolve("not-a-session") is None


@pytest.mark.parametrize("idle, valid", [
    ({"days": 30, "microseconds": -1}, True),
    ({"days": 30}, False),
])
def test_session_idle_expiry_boundary(idle, valid):
    clock = FakeClock()
    auth = _service(clock=clock)
    token = auth.claim(PW)
    clock.advance(**idle)
    assert (auth.resolve(token) is not None) is valid


def test_touch_writes_only_after_the_interval():
    clock = FakeClock()
    auth = _service(clock=clock)
    token = auth.claim(PW)
    start = clock.now
    clock.advance(minutes=59)
    session = auth.resolve(token)
    assert session.touched is False and session.last_seen_at == start
    clock.advance(minutes=1)
    session = auth.resolve(token)
    assert session.touched is True and session.last_seen_at == clock.now
    assert auth.resolve(token).touched is False


def test_use_slides_the_expiry():
    clock = FakeClock()
    auth = _service(clock=clock)
    token = auth.claim(PW)
    for _ in range(3):
        clock.advance(days=20)
        assert auth.resolve(token) is not None
    clock.advance(days=30)
    assert auth.resolve(token) is None


def test_a_failed_touch_still_resolves(caplog):
    clock = FakeClock()
    store = SqliteAuthStore(connect(":memory:"))
    auth = AuthService(store, hasher=cheap_hasher(), clock=clock)
    token = auth.claim(PW)

    def locked(session_hash, now):
        raise sqlite3.OperationalError("database is locked")

    store.touch_session = locked
    clock.advance(hours=2)
    with caplog.at_level(logging.WARNING, logger="src.auth.service"):
        session = auth.resolve(token)
    assert session is not None and session.touched is False
    assert "session_touch_failed" in [r.message for r in caplog.records]


def test_logout_ends_only_that_session():
    auth = _service()
    a = auth.claim(PW)
    b = auth.login(PW)
    auth.logout(a)
    assert auth.resolve(a) is None
    assert auth.resolve(b) is not None
    auth.logout(None)  # no cookie: a no-op


def test_change_password_requires_the_current_one():
    auth = _service()
    token = auth.claim(PW)
    with pytest.raises(WrongPassword):
        auth.change_password("wrong horse", "new password 1")
    with pytest.raises(PasswordRejected):
        auth.change_password(PW, "short")
    assert auth.resolve(token) is not None
    assert auth.login(PW) is not None


def test_change_password_ends_every_session_and_returns_a_working_token():
    auth = _service()
    a = auth.claim(PW)
    b = auth.login(PW)
    new = auth.change_password(PW, "new password 1")
    assert auth.resolve(a) is None and auth.resolve(b) is None
    assert auth.resolve(new) is not None
    assert auth.login(PW) is None
    assert auth.login("new password 1") is not None


def test_set_password_with_or_without_a_password_counts_ended_sessions():
    auth = _service()
    assert auth.set_password("first password") == 0
    assert auth.has_password() is True
    token = auth.login("first password")
    auth.login("first password")
    assert auth.set_password("second password") == 2
    assert auth.resolve(token) is None
    assert auth.login("second password") is not None
    with pytest.raises(PasswordRejected):
        auth.set_password("short")


def test_login_purges_expired_sessions_and_counts_active_ones():
    clock = FakeClock()
    conn = connect(":memory:")
    auth = _service(clock=clock, conn=conn)
    auth.claim(PW)
    clock.advance(days=31)
    assert conn.execute("SELECT COUNT(*) AS n FROM auth_sessions").fetchone()["n"] == 1
    assert auth.active_session_count() == 0
    auth.login(PW)
    auth.login(PW)
    assert conn.execute("SELECT COUNT(*) AS n FROM auth_sessions").fetchone()["n"] == 2
    assert auth.active_session_count() == 2


def test_sign_out_everywhere_keeps_the_password():
    auth = _service()
    a = auth.claim(PW)
    auth.login(PW)
    assert auth.sign_out_everywhere() == 2
    assert auth.resolve(a) is None
    assert auth.login(PW) is not None


def test_login_upgrades_an_outdated_hash():
    conn = connect(":memory:")
    _service(conn=conn).claim(PW)
    before = conn.execute("SELECT password_hash FROM auth_credential").fetchone()["password_hash"]
    stronger = _service(conn=conn, hasher=PasswordHasher(time_cost=2, memory_cost=8, parallelism=1))
    assert stronger.login(PW) is not None
    after = conn.execute("SELECT password_hash FROM auth_credential").fetchone()["password_hash"]
    assert after != before and ",t=2," in after
    assert stronger.login(PW) is not None


def test_a_failed_rehash_still_logs_in(caplog):
    store = SqliteAuthStore(connect(":memory:"))
    AuthService(store, hasher=cheap_hasher(), clock=FakeClock()).claim(PW)

    def locked(password_hash, now):
        raise sqlite3.OperationalError("database is locked")

    store.update_password_hash = locked
    auth = AuthService(store, hasher=PasswordHasher(time_cost=2, memory_cost=8, parallelism=1),
                       clock=FakeClock())
    with caplog.at_level(logging.WARNING, logger="src.auth.service"):
        assert auth.login(PW) is not None
    assert "password_rehash_failed" in [r.message for r in caplog.records]


def test_open_auth_service_uses_the_app_db(monkeypatch, tmp_path):
    from src.auth import open_auth_service
    monkeypatch.setenv("JOB_AGG_SQLITE_PATH", str(tmp_path / "app.db"))
    open_auth_service().set_password("first password")
    assert open_auth_service().has_password() is True
