"""Auth fixtures: a cheap argon2 hasher, a settable clock, and signed-in TestClients."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher


def cheap_hasher() -> PasswordHasher:
    """argon2id at minimal cost; the production defaults take ~35 ms per hash."""
    return PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)


class FakeClock:
    """A settable UTC clock for AuthService."""

    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now += timedelta(**kwargs)


def sign_in(client):
    """Give ``client`` a session on its app's auth store without going through
    /login: a stored password (a placeholder — nothing verifies it) plus a
    session row, and the cookie. Returns the client."""
    from src.auth.service import hash_token, new_token
    from src.web.auth import SESSION_COOKIE

    store = client.app.state.stores.auth
    token = new_token()
    now = datetime.now(timezone.utc)
    if not store.claim("test-placeholder-hash", hash_token(token), now):
        store.insert_session(hash_token(token), now)
    client.cookies.set(SESSION_COOKIE, token)
    return client


def signed_in_client(app, **kwargs):
    """A TestClient (same kwargs) that is already signed in."""
    from fastapi.testclient import TestClient

    return sign_in(TestClient(app, **kwargs))
