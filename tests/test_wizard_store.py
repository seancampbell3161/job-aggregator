"""Wizard UI state: what the user skipped, and the pending LLM draft.

Deliberately NOT part of the settings document — this is UI state, so it stays
out of version history and out of exported backups."""
from src.sqlite_db import connect
from src.state_sqlite import SqliteWizardStore


def _store():
    return SqliteWizardStore(connect(":memory:"))


def test_missing_key_is_none():
    assert _store().get("draft") is None


def test_round_trips_a_json_value():
    s = _store()
    s.put("draft", {"profile_md": "# Me", "filters": {"filters.titles": ["x"]}})
    assert s.get("draft")["filters"] == {"filters.titles": ["x"]}


def test_put_overwrites():
    s = _store()
    s.put("draft", {"a": 1})
    s.put("draft", {"a": 2})
    assert s.get("draft") == {"a": 2}


def test_delete_is_idempotent():
    s = _store()
    s.put("draft", {"a": 1})
    s.put("other", {"b": 2})
    s.delete("draft")
    s.delete("draft")
    assert s.get("draft") is None
    assert s.get("other") == {"b": 2}  # second key survives


def test_skip_and_unskip():
    s = _store()
    assert s.skipped() == set()
    s.skip("notifications")
    s.skip("llm")
    assert s.skipped() == {"notifications", "llm"}
    s.unskip("llm")
    assert s.skipped() == {"notifications"}


def test_skipping_twice_does_not_duplicate():
    s = _store()
    s.skip("llm")
    s.skip("llm")
    assert s.get("skipped") == ["llm"]  # raw storage, not skipped()
    assert s.skipped() == {"llm"}


def test_unreadable_value_reads_as_none():
    """A hand-edited or truncated row must not 500 the wizard."""
    s = _store()
    s._conn.execute(
        "INSERT INTO wizard_ui (key, value, updated_at) VALUES ('draft', '{not json', 'now')"
    )
    assert s.get("draft") is None


def test_stores_exposes_the_wizard_store():
    from tests.sqlite_helpers import sqlite_stores
    stores = sqlite_stores(connect(":memory:"))
    assert isinstance(stores.wizard, SqliteWizardStore)
