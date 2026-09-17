"""SqliteAuthStore: the single credential row and session rows."""
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from src.auth.store import SessionRow, SqliteAuthStore
from src.sqlite_db import connect

T0 = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)


def _store(path: str = ":memory:") -> SqliteAuthStore:
    return SqliteAuthStore(connect(path))


def test_empty_store():
    s = _store()
    assert s.password_hash() is None
    assert s.get_session("h1") is None
    assert s.count_sessions(cutoff=T0) == 0


def test_claim_stores_the_password_and_first_session_once():
    s = _store()
    assert s.claim("hash-1", "sess-1", T0) is True
    assert s.password_hash() == "hash-1"
    assert s.get_session("sess-1") == SessionRow(token_hash="sess-1", created_at=T0, last_seen_at=T0)
    assert s.claim("hash-2", "sess-2", T0) is False
    assert s.password_hash() == "hash-1"
    assert s.get_session("sess-2") is None


def test_the_schema_allows_only_one_credential_row():
    conn = connect(":memory:")
    conn.execute("INSERT INTO auth_credential (id, password_hash, updated_at) VALUES (1, 'a', 'x')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO auth_credential (id, password_hash, updated_at) VALUES (2, 'b', 'x')")


def test_update_password_hash_keeps_sessions():
    s = _store()
    s.claim("hash-1", "sess-1", T0)
    s.update_password_hash("hash-2", T0 + timedelta(minutes=1))
    assert s.password_hash() == "hash-2"
    assert s.get_session("sess-1") is not None


def test_replace_password_inserts_when_absent_and_ends_every_session():
    s = _store()
    assert s.replace_password("hash-1", T0) == 0
    assert s.password_hash() == "hash-1"
    s.insert_session("a", T0)
    s.insert_session("b", T0)
    assert s.replace_password("hash-2", T0, new_session_hash="c") == 2
    assert s.password_hash() == "hash-2"
    assert s.get_session("a") is None and s.get_session("b") is None
    assert s.get_session("c") is not None


def test_replace_password_is_atomic(tmp_path):
    path = str(tmp_path / "auth.db")
    seed = _store(path)
    seed.claim("hash-1", "sess-1", T0)

    class Boom(SqliteAuthStore):
        def _insert_session(self, session_hash, now):
            raise RuntimeError("injected")

    boom = Boom(connect(path))
    with pytest.raises(RuntimeError, match="injected"):
        boom.replace_password("hash-2", T0, new_session_hash="sess-2")
    assert seed.password_hash() == "hash-1"
    assert seed.get_session("sess-1") is not None


def test_touch_delete_and_delete_all():
    s = _store()
    s.insert_session("a", T0)
    s.insert_session("b", T0)
    later = T0 + timedelta(hours=2)
    s.touch_session("a", later)
    assert s.get_session("a") == SessionRow(token_hash="a", created_at=T0, last_seen_at=later)
    assert s.delete_session("a") is True
    assert s.delete_session("a") is False
    assert s.delete_all_sessions() == 1
    assert s.get_session("b") is None


def test_purge_and_count_split_at_the_cutoff():
    s = _store()
    s.insert_session("at-cutoff", T0)
    s.insert_session("before", T0 - timedelta(microseconds=1))
    s.insert_session("after", T0 + timedelta(microseconds=1))
    assert s.count_sessions(cutoff=T0) == 1
    assert s.purge_sessions(cutoff=T0) == 2
    assert s.get_session("after") is not None
    assert s.get_session("at-cutoff") is None and s.get_session("before") is None


def test_two_connections_on_one_file_see_each_others_writes(tmp_path):
    path = str(tmp_path / "shared.db")
    web, cli = _store(path), _store(path)
    web.claim("hash-1", "sess-1", T0)
    assert cli.password_hash() == "hash-1"
    assert cli.replace_password("hash-2", T0) == 1
    assert web.get_session("sess-1") is None
    assert web.password_hash() == "hash-2"
