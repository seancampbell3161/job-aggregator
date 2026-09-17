"""ConfigService lifecycle: history/restore, read-modify-write, secrets bootstrap."""
import pytest

from src.settings.errors import NotConfigured, SettingsInvalid, StaleWrite
from src.settings.service import ConfigService
from src.settings.store import SqliteSettingsStore
from src.sqlite_db import connect


def _svc(env=None):
    store = SqliteSettingsStore(connect(":memory:"))
    return ConfigService(store, env={} if env is None else env), store


def test_restore_inserts_a_new_row_with_restore_source():
    svc, store = _svc()
    v1 = svc.save_settings({"schedules": {"ats_minutes": 3}}, source="cli")
    svc.save_settings({"schedules": {"ats_minutes": 9}}, source="cli")
    v3 = svc.restore(v1)
    assert v3 > v1
    row = store.latest_settings()
    assert (row.id, row.source, row.note) == (v3, "restore", f"restored version {v1}")
    assert svc.snapshot().cfg.schedules.ats_minutes == 3
    assert svc.versions()[0].id == v3
    assert len(svc.versions(limit=None)) == 3


def test_restore_refuses_unknown_or_invalid_versions():
    svc, store = _svc()
    with pytest.raises(SettingsInvalid, match="no settings version"):
        svc.restore(42)
    bad = store.insert_settings(doc={"schedules": {"ats_minutes": 0}}, source="ui",
                                note=None, schema_version=1)
    with pytest.raises(SettingsInvalid):
        svc.restore(bad)


def test_current_doc_is_the_effective_canonical_doc_without_overlays():
    svc, store = _svc({"JOB_AGG_OLLAMA_HOST": "https://ollama.com",
                       "JOB_AGG_NTFY_TOPIC_URL": "https://ntfy.sh/x"})
    assert svc.current_doc() is None
    vid = svc.save_settings({"schedules": {"ats_minutes": 4}}, source="cli")
    store.insert_settings(doc={"schedules": {"ats_minutes": "bad"}}, source="ui",
                          note=None, schema_version=1)
    assert svc.current_doc() == (vid, {"schedules": {"ats_minutes": 4}})


def test_update_settings_applies_the_mutation_with_its_note():
    svc, store = _svc()
    svc.save_settings({}, source="cli")

    def mutate(doc):
        doc.setdefault("sources", {}).setdefault("lever", []).append("acme")
        return "added lever:acme"

    vid = svc.update_settings(mutate, source="cli")
    row = store.latest_settings()
    assert (row.id, row.note, row.source) == (vid, "added lever:acme", "cli")
    assert svc.snapshot().cfg.sources.lever == ["acme"]


def test_update_settings_without_a_change_writes_nothing():
    svc, store = _svc()
    svc.save_settings({}, source="cli")
    gen = store.generation()
    assert svc.update_settings(lambda doc: None, source="cli") is None
    assert store.generation() == gen


def test_update_settings_requires_setup():
    svc, _ = _svc()
    with pytest.raises(NotConfigured):
        svc.update_settings(lambda doc: "x", source="cli")


def test_update_settings_retries_once_on_a_concurrent_write():
    svc, store = _svc()
    svc.save_settings({}, source="cli")
    seen_docs = []

    def mutate(doc):
        seen_docs.append(dict(doc))
        if len(seen_docs) == 1:  # another writer lands between read and write
            ConfigService(store, env={}).save_settings({"schedules": {"ats_minutes": 2}}, source="ui")
        doc.setdefault("sources", {})["ashby"] = ["x"]
        return "add ashby:x"

    svc.update_settings(mutate, source="cli")
    assert len(seen_docs) == 2
    assert seen_docs[1] == {"schedules": {"ats_minutes": 2}}
    cfg = svc.snapshot().cfg
    assert cfg.sources.ashby == ["x"]
    assert cfg.schedules.ats_minutes == 2  # the concurrent write survived


def test_update_settings_gives_up_after_a_second_conflict():
    svc, store = _svc()
    svc.save_settings({}, source="cli")

    def mutate(doc):
        ConfigService(store, env={}).save_settings({}, source="ui")
        return "never lands"

    with pytest.raises(StaleWrite):
        svc.update_settings(mutate, source="cli")


def test_ensure_signing_secret_generates_once():
    svc, store = _svc()
    svc.ensure_signing_secret()
    first = store.get_secret("tailor_signing_secret")
    assert first and len(first) >= 40
    svc.ensure_signing_secret()
    assert store.get_secret("tailor_signing_secret") == first


def test_ensure_signing_secret_respects_the_env():
    svc, store = _svc({"JOB_AGG_TAILOR_SIGNING_SECRET": "from-env"})
    svc.ensure_signing_secret()
    assert store.get_secret("tailor_signing_secret") is None


def test_ensure_signing_secret_converges_across_processes(tmp_path):
    path = str(tmp_path / "s.db")
    a = ConfigService(SqliteSettingsStore(connect(path)), env={})
    b = ConfigService(SqliteSettingsStore(connect(path)), env={})
    a.ensure_signing_secret()
    b.ensure_signing_secret()
    a.save_settings({}, source="cli")
    secret = a.snapshot().cfg.secrets.tailor_signing_secret
    assert secret and secret == b.snapshot().cfg.secrets.tailor_signing_secret


def test_import_env_secrets_copies_non_empty_values_only():
    svc, store = _svc({"JOB_AGG_NTFY_TOPIC_URL": "https://n", "JOB_AGG_HEARTBEAT_URL": "",
                       "JOB_AGG_UNRELATED": "x"})
    assert svc.import_env_secrets() == ["ntfy_topic_url"]
    assert store.all_secrets() == {"ntfy_topic_url": "https://n"}


def test_canonicalize_returns_the_stored_form_without_writing():
    svc, store = _svc()
    doc = svc.canonicalize({"schedules": {"ats_minutes": 10, "slow_minutes": 20}, "bogus": 1})
    assert doc == {"schedules": {"slow_minutes": 20}}
    assert store.generation() == 0


def test_canonicalize_raises_for_an_invalid_document():
    svc, _ = _svc()
    with pytest.raises(SettingsInvalid):
        svc.canonicalize({"schedules": {"ats_minutes": 0}})


def test_version_doc_is_the_canonical_document_of_a_valid_version():
    svc, store = _svc()
    vid = svc.save_settings({"schedules": {"slow_minutes": 20}}, source="cli")
    bad = store.insert_settings(doc={"schedules": {"ats_minutes": 0}}, source="ui",
                                note=None, schema_version=1)
    assert svc.version_doc(vid) == {"schedules": {"slow_minutes": 20}}
    assert svc.version_doc(bad) is None
    assert svc.version_doc(999) is None
