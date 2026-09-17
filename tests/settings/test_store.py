"""SqliteSettingsStore: append-only settings versions and documents, secrets,
and the generation counter bumped atomically with every write."""
import sqlite3

import pytest

from src.settings.errors import StaleWrite
from src.settings.store import NewDocument, NewSettings, SqliteSettingsStore
from src.sqlite_db import connect


def _store(path: str = ":memory:") -> SqliteSettingsStore:
    return SqliteSettingsStore(connect(path))


def test_empty_store():
    s = _store()
    assert s.generation() == 0
    assert s.latest_settings() is None
    assert s.settings_versions() == []
    assert s.latest_document("profile") is None
    assert s.all_secrets() == {}


def test_insert_settings_bumps_generation_and_becomes_latest():
    s = _store()
    first = s.insert_settings(doc={"a": 1}, source="cli", note="one", schema_version=1)
    second = s.insert_settings(doc={"a": 2}, source="import", note=None, schema_version=1)
    assert second > first
    assert s.generation() == 2
    latest = s.latest_settings()
    assert (latest.id, latest.doc, latest.source, latest.note, latest.schema_version) == (
        second, {"a": 2}, "import", None, 1,
    )
    assert latest.created_at.endswith("+00:00")
    assert [r.id for r in s.settings_versions()] == [second, first]
    assert s.get_settings_version(first).doc == {"a": 1}
    assert s.get_settings_version(999) is None


def test_settings_versions_limit():
    s = _store()
    ids = [s.insert_settings(doc={}, source="cli", note=None, schema_version=1) for _ in range(3)]
    assert [r.id for r in s.settings_versions(limit=2)] == [ids[2], ids[1]]
    assert len(s.settings_versions(limit=None)) == 3


def test_stale_base_version_raises_and_writes_nothing():
    s = _store()
    v1 = s.insert_settings(doc={}, source="cli", note=None, schema_version=1)
    s.insert_settings(doc={"x": 1}, source="cli", note=None, schema_version=1, base_version_id=v1)
    gen = s.generation()
    with pytest.raises(StaleWrite):
        s.insert_settings(doc={"x": 2}, source="cli", note=None, schema_version=1, base_version_id=v1)
    assert s.generation() == gen
    assert len(s.settings_versions(limit=None)) == 2


def test_base_version_against_an_empty_table_is_stale():
    with pytest.raises(StaleWrite):
        _store().insert_settings(doc={}, source="cli", note=None, schema_version=1, base_version_id=7)


def test_documents_latest_per_kind_and_stale_base():
    s = _store()
    p1 = s.insert_document(kind="profile", body="v1", source="import")
    s.insert_document(kind="resume_text", body="r1", source="import")
    p2 = s.insert_document(kind="profile", body="v2", source="cli", base_document_id=p1)
    assert s.latest_document("profile").id == p2
    assert s.latest_document("profile").body == "v2"
    assert s.latest_document("resume_text").body == "r1"
    with pytest.raises(StaleWrite):
        s.insert_document(kind="profile", body="v3", source="cli", base_document_id=p1)
    assert s.latest_document("profile").body == "v2"


def test_insert_bundle_is_one_transaction_and_one_generation():
    s = _store()
    vid, doc_ids = s.insert_bundle(
        source="import",
        settings=NewSettings(doc={"a": 1}, note="bundle", schema_version=1),
        documents=[NewDocument(kind="profile", body="p"), NewDocument(kind="kit_facts", body="[]")],
    )
    assert s.generation() == 1
    assert s.latest_settings().id == vid
    assert [s.latest_document(k).id for k in ("profile", "kit_facts")] == doc_ids
    assert s.latest_document("profile").source == "import"


def test_insert_bundle_stale_document_rolls_back_the_settings_row():
    s = _store()
    old = s.insert_document(kind="profile", body="v1", source="cli")
    s.insert_document(kind="profile", body="v2", source="cli")
    gen = s.generation()
    with pytest.raises(StaleWrite):
        s.insert_bundle(
            source="ui",
            settings=NewSettings(doc={}, note=None, schema_version=1),
            documents=[NewDocument(kind="profile", body="v3", base_document_id=old)],
        )
    assert s.latest_settings() is None
    assert s.generation() == gen


def test_secrets_crud():
    s = _store()
    s.put_secret("ntfy_topic_url", "https://ntfy.sh/a")
    s.put_secret("ntfy_topic_url", "https://ntfy.sh/b")
    assert s.get_secret("ntfy_topic_url") == "https://ntfy.sh/b"
    assert s.get_secret("heartbeat_url") is None
    assert s.stored_secret_names() == {"ntfy_topic_url"}
    assert s.all_secrets() == {"ntfy_topic_url": "https://ntfy.sh/b"}
    gen = s.generation()
    assert s.delete_secret("ntfy_topic_url") is True
    assert s.generation() > gen
    assert s.delete_secret("ntfy_topic_url") is False
    assert s.all_secrets() == {}


def test_put_secret_if_absent_keeps_the_first_value():
    s = _store()
    assert s.put_secret_if_absent("tailor_signing_secret", "first") is True
    assert s.put_secret_if_absent("tailor_signing_secret", "second") is False
    assert s.get_secret("tailor_signing_secret") == "first"


def test_failure_mid_transaction_leaves_rows_and_generation_unchanged():
    class Boom(SqliteSettingsStore):
        def _bump_generation(self) -> None:
            raise RuntimeError("injected")

    s = Boom(connect(":memory:"))
    with pytest.raises(RuntimeError, match="injected"):
        s.insert_settings(doc={"a": 1}, source="cli", note=None, schema_version=1)
    assert s.latest_settings() is None
    assert s.generation() == 0
    with pytest.raises(RuntimeError, match="injected"):
        s.put_secret("ntfy_topic_url", "x")
    assert s.get_secret("ntfy_topic_url") is None


def test_two_connections_on_one_file_see_each_others_writes(tmp_path):
    path = str(tmp_path / "shared.db")
    a, b = _store(path), _store(path)
    a.insert_settings(doc={"from": "a"}, source="cli", note=None, schema_version=1)
    assert b.generation() == 1
    assert b.latest_settings().doc == {"from": "a"}
    b.put_secret("heartbeat_url", "https://hc.test/1")
    assert a.get_secret("heartbeat_url") == "https://hc.test/1"
    assert a.generation() == 2


def test_build_stores_wires_the_settings_store():
    from src.stores import build_stores
    stores = build_stores()
    assert isinstance(stores.settings, SqliteSettingsStore)
    assert stores.settings._conn is not stores.seen._conn


def test_read_is_reentrant_and_consistent(tmp_path):
    path = str(tmp_path / "read.db")
    s = _store(path)
    s.insert_settings(doc={"a": 1}, source="cli", note=None, schema_version=1)

    # A second connection to the same file, opened up front (not while our
    # read is open) so its own schema/pragma setup can't contend with the
    # lock we're about to take.
    other = connect(path)
    other.execute("PRAGMA busy_timeout=100")

    with s.read():
        assert s.generation() == 1
        with s.read():  # nested: reentrant, no re-BEGIN, no premature commit
            assert s.latest_settings().doc == {"a": 1}
        assert s.generation() == 1  # the inner exit didn't end the outer read

        # Our SHARED lock (held since the first SELECT above, for the whole
        # `with s.read():` body) must block a concurrent writer on another
        # connection from committing — with a short busy_timeout it gives up
        # and raises rather than landing mid-read.
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            other.execute("BEGIN IMMEDIATE")
            other.execute(
                "INSERT INTO settings_versions (created_at, source, note, schema_version, doc) "
                "VALUES ('x', 'cli', NULL, 1, '{}')"
            )
            other.execute("COMMIT")
        # The failed COMMIT left `other`'s transaction open (mid-upgrade to an
        # EXCLUSIVE lock); release it before anyone else tries a fresh read.
        other.execute("ROLLBACK")

    assert s.generation() == 1  # the blocked write never landed
    other.close()
