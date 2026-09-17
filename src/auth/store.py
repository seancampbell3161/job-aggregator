"""SQLite persistence for the web UI login: the admin password hash (one
row) and sessions (keyed by the SHA-256 of the cookie token). Persistence
only — AuthService decides what is valid.

Runs on its own connection (see build_stores): the multi-statement writes
use BEGIN IMMEDIATE, which would collide with another store's transaction on
a shared connection. Every method takes the store lock, so one thread's
statement can never land inside another thread's open transaction here.

Timestamps are UTC ISO-8601 with fixed microsecond precision, so SQL string
comparison orders them correctly. Writes here never bump config_generation:
nothing caches these tables."""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Iterator


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="microseconds")


@dataclass(frozen=True)
class SessionRow:
    token_hash: str
    created_at: datetime
    last_seen_at: datetime


class SqliteAuthStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._lock = threading.RLock()

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield
                self._conn.execute("COMMIT")
            except BaseException:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise

    # -- credential ------------------------------------------------------------

    def password_hash(self) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT password_hash FROM auth_credential WHERE id = 1"
            ).fetchone()
        return row["password_hash"] if row else None

    def claim(self, password_hash: str, session_hash: str, now: datetime) -> bool:
        """Store the first password and its first session in one transaction.
        False — nothing written — when a password already exists."""
        with self._transaction():
            cur = self._conn.execute(
                "INSERT INTO auth_credential (id, password_hash, updated_at) VALUES (1, ?, ?) "
                "ON CONFLICT(id) DO NOTHING",
                (password_hash, _iso(now)),
            )
            if cur.rowcount != 1:
                return False
            self._insert_session(session_hash, now)
        return True

    def update_password_hash(self, password_hash: str, now: datetime) -> None:
        """Re-hash the same password (new parameters); sessions are kept."""
        with self._lock:
            self._conn.execute(
                "UPDATE auth_credential SET password_hash = ?, updated_at = ? WHERE id = 1",
                (password_hash, _iso(now)),
            )

    def replace_password(
        self, password_hash: str, now: datetime, *, new_session_hash: str | None = None,
    ) -> int:
        """Set the password (inserting or replacing it), end every session, and
        optionally start one new session — one transaction. Returns the number
        of sessions ended."""
        with self._transaction():
            self._conn.execute(
                "INSERT INTO auth_credential (id, password_hash, updated_at) VALUES (1, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET password_hash = excluded.password_hash, "
                "updated_at = excluded.updated_at",
                (password_hash, _iso(now)),
            )
            ended = self._conn.execute("DELETE FROM auth_sessions").rowcount
            if new_session_hash is not None:
                self._insert_session(new_session_hash, now)
        return ended

    # -- sessions --------------------------------------------------------------

    def _insert_session(self, session_hash: str, now: datetime) -> None:
        self._conn.execute(
            "INSERT INTO auth_sessions (token_hash, created_at, last_seen_at) VALUES (?, ?, ?)",
            (session_hash, _iso(now), _iso(now)),
        )

    def insert_session(self, session_hash: str, now: datetime) -> None:
        with self._lock:
            self._insert_session(session_hash, now)

    def get_session(self, session_hash: str) -> SessionRow | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT token_hash, created_at, last_seen_at FROM auth_sessions "
                "WHERE token_hash = ?",
                (session_hash,),
            ).fetchone()
        if row is None:
            return None
        return SessionRow(
            token_hash=row["token_hash"],
            created_at=datetime.fromisoformat(row["created_at"]),
            last_seen_at=datetime.fromisoformat(row["last_seen_at"]),
        )

    def touch_session(self, session_hash: str, now: datetime) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE auth_sessions SET last_seen_at = ? WHERE token_hash = ?",
                (_iso(now), session_hash),
            )

    def delete_session(self, session_hash: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM auth_sessions WHERE token_hash = ?", (session_hash,)
            )
        return cur.rowcount == 1

    def delete_all_sessions(self) -> int:
        with self._lock:
            return self._conn.execute("DELETE FROM auth_sessions").rowcount

    def purge_sessions(self, *, cutoff: datetime) -> int:
        """Delete sessions last seen at or before ``cutoff`` (expired)."""
        with self._lock:
            return self._conn.execute(
                "DELETE FROM auth_sessions WHERE last_seen_at <= ?", (_iso(cutoff),)
            ).rowcount

    def count_sessions(self, *, cutoff: datetime) -> int:
        """Count sessions last seen after ``cutoff`` (still active)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM auth_sessions WHERE last_seen_at > ?", (_iso(cutoff),)
            ).fetchone()
        return int(row["n"])
