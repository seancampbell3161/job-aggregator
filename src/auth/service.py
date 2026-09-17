"""AuthService — the web UI's single admin password and its sessions.

Session tokens are random (secrets.token_urlsafe(32)); only their SHA-256 is
stored, so a copy of the database holds no usable cookie. A session expires
after SESSION_IDLE_TTL without use; using it refreshes last_seen_at at most
once per SESSION_TOUCH_INTERVAL, so browsing doesn't write on every request."""
from __future__ import annotations

import hashlib
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from argon2 import PasswordHasher

from src.auth.errors import AlreadyClaimed, WrongPassword
from src.auth.passwords import Passwords, validate_new_password
from src.auth.store import SqliteAuthStore

log = logging.getLogger(__name__)

SESSION_IDLE_TTL = timedelta(days=30)
SESSION_TOUCH_INTERVAL = timedelta(hours=1)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_token() -> str:
    return secrets.token_urlsafe(32)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class Session:
    token_hash: str
    created_at: datetime
    last_seen_at: datetime
    touched: bool  # last_seen_at was just written: re-issue the cookie


class AuthService:
    def __init__(
        self,
        store: SqliteAuthStore,
        *,
        hasher: PasswordHasher | None = None,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._store = store
        self._passwords = Passwords(hasher)
        self._clock = clock

    def _now(self) -> datetime:
        return self._clock().astimezone(timezone.utc)

    def has_password(self) -> bool:
        return self._store.password_hash() is not None

    def claim(self, password: str) -> str:
        """Set the first password and start its session; returns the token.
        Raises AlreadyClaimed when a password exists (the store settles races)."""
        validate_new_password(password)
        token = new_token()
        if not self._store.claim(self._passwords.hash(password), hash_token(token), self._now()):
            raise AlreadyClaimed("this instance already has a password")
        return token

    def login(self, password: str) -> str | None:
        stored = self._store.password_hash()
        if stored is None or not self._passwords.verify(stored, password):
            return None
        now = self._now()
        if self._passwords.needs_rehash(stored):
            try:
                self._store.update_password_hash(self._passwords.hash(password), now)
            except Exception as exc:  # noqa: BLE001 — the password verified; the upgrade can wait
                log.warning("password_rehash_failed", extra={"error": str(exc)})
        self._store.purge_sessions(cutoff=now - SESSION_IDLE_TTL)
        token = new_token()
        self._store.insert_session(hash_token(token), now)
        return token

    def resolve(self, token: str | None) -> Session | None:
        if not token:
            return None
        token_hash = hash_token(token)
        row = self._store.get_session(token_hash)
        if row is None:
            return None
        now = self._now()
        idle = now - row.last_seen_at
        if idle >= SESSION_IDLE_TTL:
            return None
        if idle < SESSION_TOUCH_INTERVAL:
            return Session(token_hash, row.created_at, row.last_seen_at, touched=False)
        try:
            self._store.touch_session(token_hash, now)
        except Exception as exc:  # noqa: BLE001 — the session is valid; refreshing it can wait
            log.warning("session_touch_failed", extra={"error": str(exc)})
            return Session(token_hash, row.created_at, row.last_seen_at, touched=False)
        return Session(token_hash, row.created_at, now, touched=True)

    def logout(self, token: str | None) -> None:
        if token:
            self._store.delete_session(hash_token(token))

    def change_password(self, current: str, new: str) -> str:
        """Replace the password, end every session, and return a new session
        token for the browser that made the change."""
        stored = self._store.password_hash()
        if stored is None or not self._passwords.verify(stored, current):
            raise WrongPassword("the current password is incorrect")
        validate_new_password(new)
        token = new_token()
        self._store.replace_password(
            self._passwords.hash(new), self._now(), new_session_hash=hash_token(token),
        )
        return token

    def set_password(self, new: str) -> int:
        """The CLI path: set the password with or without an existing one and
        end every session. Returns the number of sessions ended."""
        validate_new_password(new)
        return self._store.replace_password(self._passwords.hash(new), self._now())

    def sign_out_everywhere(self) -> int:
        return self._store.delete_all_sessions()

    def active_session_count(self) -> int:
        return self._store.count_sessions(cutoff=self._now() - SESSION_IDLE_TTL)
