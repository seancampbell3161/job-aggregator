"""ConfigService core: snapshots, validated saves, degraded fallback, secrets."""
import sqlite3
import threading
from pathlib import Path

import pytest
import yaml

from src.config import AppConfig, Secrets
from src.settings.errors import SettingsInvalid, StaleWrite
from src.settings.service import SECRET_NAMES, ConfigService, canonical_doc, secret_env_var
from src.settings.store import SqliteSettingsStore
from src.sqlite_db import connect
from tests.settings_helpers import make_service


def _svc(env=None):
    store = SqliteSettingsStore(connect(":memory:"))
    return ConfigService(store, env={} if env is None else env), store


# -- setup state ----------------------------------------------------------------

def test_not_set_up_snapshot_is_none():
    assert make_service().snapshot() is None


def test_empty_document_is_a_valid_setup():
    snap = make_service({}).snapshot()
    assert snap is not None
    assert snap.cfg.schedules.ats_minutes == 10
    assert snap.degraded is None
    assert snap.documents.profile is None


# -- validated, canonical saves ---------------------------------------------------

def test_invalid_save_writes_nothing():
    svc, store = _svc()
    with pytest.raises(SettingsInvalid) as exc:
        svc.save_settings({"schedules": {"ats_minutes": 0}}, source="cli")
    assert exc.value.errors[0]["loc"] == "schedules.ats_minutes"
    assert store.latest_settings() is None
    assert store.generation() == 0


def test_secrets_key_is_rejected():
    svc, store = _svc()
    with pytest.raises(SettingsInvalid, match="secrets"):
        svc.save_settings({"secrets": {"ntfy_topic_url": "x"}}, source="cli")
    assert store.latest_settings() is None


def test_unknown_source_is_rejected():
    svc, _ = _svc()
    with pytest.raises(ValueError, match="source"):
        svc.save_settings({}, source="yaml")


def test_stored_document_is_canonical():
    svc, store = _svc()
    svc.save_settings(
        {"schedules": {"ats_minutes": 10, "slow_minutes": 20},
         "filters": {"location": {"allowed_countries": ["uk"]}},
         "bogus": 1},
        source="cli",
    )
    assert store.latest_settings().doc == {
        "filters": {"location": {"allowed_countries": ["GB"]}},
        "schedules": {"slow_minutes": 20},
    }


def test_canonical_doc_round_trips_the_example_config():
    raw = yaml.safe_load(Path("config.example.yaml").read_text())
    cfg = AppConfig.model_validate(raw)
    assert AppConfig.model_validate(canonical_doc(cfg)).model_dump() == cfg.model_dump()


def test_canonical_doc_round_trips_nested_shapes():
    raw = {
        "filters": {"titles": ["engineer"],
                    "location": {"remote_must_be_us": False, "allowed_cities": ["berlin"]}},
        "quiet_hours": {"timezone": "Europe/Berlin", "start": "22:00", "end": "07:30"},
        "sources": {
            "workday": [{"tenant": "acme", "region": "wd5", "site": "Ext"}],
            "hiringcafe": {"extra_queries": ["plain", {"query": "q", "location": "de"}]},
            "hn_who_is_hiring": {"enabled": False},
        },
        "http": {"user_agent": "me"},
    }
    with pytest.warns(DeprecationWarning):
        cfg = AppConfig.model_validate(raw)
    doc = canonical_doc(cfg)
    assert "remote_must_be_us" not in doc["filters"]["location"]
    assert AppConfig.model_validate(doc).model_dump() == cfg.model_dump()


def test_stale_base_version_raises():
    svc, _ = _svc()
    v1 = svc.save_settings({}, source="cli")
    svc.save_settings({"schedules": {"ats_minutes": 5}}, source="cli", base_version_id=v1)
    with pytest.raises(StaleWrite):
        svc.save_settings({"schedules": {"ats_minutes": 6}}, source="cli", base_version_id=v1)


def test_unknown_timezone_is_a_field_error_on_save():
    svc, store = _svc()
    with pytest.raises(SettingsInvalid) as exc:
        svc.save_settings(
            {"quiet_hours": {"timezone": "Mars/Olympus_Mons", "start": "22:00", "end": "07:00"}},
            source="cli",
        )
    assert exc.value.errors[0]["loc"] == "quiet_hours.timezone"
    assert store.latest_settings() is None


# -- snapshots --------------------------------------------------------------------

def test_snapshot_is_cached_until_generation_moves(monkeypatch):
    svc, store = _svc()
    svc.save_settings({}, source="cli")
    builds = []
    real_build = svc._build
    monkeypatch.setattr(svc, "_build", lambda gen: builds.append(gen) or real_build(gen))
    first = svc.snapshot()
    assert svc.snapshot() is first
    assert len(builds) == 1
    svc.save_settings({"schedules": {"ats_minutes": 3}}, source="cli")
    second = svc.snapshot()
    assert second is not first
    assert second.cfg.schedules.ats_minutes == 3
    assert second.generation == store.generation()
    assert len(builds) == 2


def test_writes_through_another_connection_are_seen(tmp_path):
    path = str(tmp_path / "s.db")
    writer = ConfigService(SqliteSettingsStore(connect(path)), env={})
    reader = ConfigService(SqliteSettingsStore(connect(path)), env={})
    assert reader.snapshot() is None
    writer.save_settings({"schedules": {"ats_minutes": 4}}, source="cli")
    assert reader.snapshot().cfg.schedules.ats_minutes == 4


def test_snapshot_applies_the_user_agent_override(monkeypatch):
    calls = []
    monkeypatch.setattr("src.settings.service.set_user_agent", calls.append)
    svc, _ = _svc()
    svc.save_settings({"http": {"user_agent": "me/1"}}, source="cli")
    svc.snapshot()
    assert calls == ["me/1"]


def test_snapshot_never_caches_an_uncommitted_write():
    """A writer's insert is visible on the shared connection before its
    transaction resolves. snapshot() must never read it: it has to block on
    the store's write lock and, once the write rolls back, see only the last
    committed state — never a generation/version that doesn't exist."""

    class _BlockingStore(SqliteSettingsStore):
        def __init__(self, conn):
            super().__init__(conn)
            self.insert_done = threading.Event()
            self.release = threading.Event()
            self._armed = False  # the baseline save below must commit normally

        def _bump_generation(self):
            if not self._armed:
                super()._bump_generation()
                return
            self.insert_done.set()
            assert self.release.wait(timeout=5), "test bug: release was never set"
            raise sqlite3.OperationalError("simulated failure after insert, before commit")

    store = _BlockingStore(connect(":memory:"))
    svc = ConfigService(store, env={})
    svc.save_settings({"schedules": {"ats_minutes": 7}}, source="cli")  # the only committed version
    store._armed = True

    writer_result: dict = {}

    def writer():
        try:
            svc.save_settings({"schedules": {"ats_minutes": 3}}, source="cli")
        except sqlite3.OperationalError:
            writer_result["raised"] = True

    t = threading.Thread(target=writer, daemon=True)
    t.start()
    assert store.insert_done.wait(timeout=5), "writer never reached the insert"

    reader_result: dict = {}

    def reader():
        reader_result["snapshot"] = svc.snapshot()

    r = threading.Thread(target=reader, daemon=True)
    r.start()
    r.join(timeout=0.2)
    assert r.is_alive(), "reader did not block on the writer's open transaction"

    store.release.set()
    t.join(timeout=5)
    r.join(timeout=5)
    assert not t.is_alive() and not r.is_alive(), "a thread failed to finish — see stderr"

    assert writer_result.get("raised") is True
    snap = reader_result["snapshot"]
    assert snap is not None
    assert snap.cfg.schedules.ats_minutes == 7  # never the uncommitted 3

    # And a fresh snapshot() call afterwards agrees.
    final = svc.snapshot()
    assert final.cfg.schedules.ats_minutes == 7


# -- documents --------------------------------------------------------------------

def test_save_document_validates_and_the_snapshot_carries_the_latest():
    svc, _ = _svc()
    svc.save_settings({}, source="cli")
    first = svc.save_document("profile", "v1", source="import")
    svc.save_document("profile", "v2", source="cli", base_document_id=first)
    assert svc.snapshot().documents.profile == "v2"
    with pytest.raises(StaleWrite):
        svc.save_document("profile", "v3", source="cli", base_document_id=first)
    with pytest.raises(SettingsInvalid):
        svc.save_document("resume_content", "{broken", source="cli")
    assert svc.snapshot().documents.resume_content is None


def test_save_bundle_reports_every_error_and_writes_nothing():
    svc, store = _svc()
    with pytest.raises(SettingsInvalid) as exc:
        svc.save_bundle({"schedules": {"ats_minutes": 0}}, {"profile": "", "evidence": "{}"}, source="import")
    assert [e["loc"] for e in exc.value.errors] == ["schedules.ats_minutes", "profile", "evidence"]
    assert store.generation() == 0


def test_save_bundle_writes_everything_in_one_generation():
    svc, store = _svc()
    vid, doc_ids = svc.save_bundle({}, {"profile": "p", "resume_text": "r"}, source="import", note="n")
    assert store.generation() == 1
    assert set(doc_ids) == {"profile", "resume_text"}
    snap = svc.snapshot()
    assert snap.version_id == vid
    assert snap.documents.resume_text == "r"


# -- degraded fallback ---------------------------------------------------------------

def test_invalid_newest_version_falls_back_and_reports_degraded(caplog):
    svc, store = _svc()
    good = svc.save_settings({"schedules": {"ats_minutes": 7}}, source="cli")
    bad = store.insert_settings(doc={"schedules": {"ats_minutes": "often"}}, source="ui",
                                note=None, schema_version=1)
    with caplog.at_level("ERROR", logger="src.settings.service"):
        snap = svc.snapshot()
    assert snap.version_id == good
    assert snap.cfg.schedules.ats_minutes == 7
    assert snap.degraded.invalid_version_id == bad
    assert snap.degraded.errors[0]["loc"] == "schedules.ats_minutes"
    assert any(r.message == "settings_version_invalid" for r in caplog.records)


def test_row_with_invalid_json_degrades_and_history_still_lists_it():
    svc, store = _svc()
    good = svc.save_settings({"schedules": {"ats_minutes": 7}}, source="cli")
    with store._write():
        store._conn.execute(
            "INSERT INTO settings_versions (created_at, source, note, schema_version, doc) "
            "VALUES ('2026-09-17T00:00:00+00:00', 'ui', 'corrupt', 1, '{not json')"
        )
    bad = store.latest_settings().id
    snap = svc.snapshot()
    assert snap.version_id == good
    assert snap.cfg.schedules.ats_minutes == 7
    assert snap.degraded.invalid_version_id == bad
    assert snap.degraded.errors == [
        {"loc": "", "msg": f"settings version {bad} is not valid JSON"}
    ]
    rows = svc.versions()
    assert [(r.id, r.doc) for r in rows] == [(bad, None), (good, {"schedules": {"ats_minutes": 7}})]


def test_update_settings_warns_when_it_replaces_an_invalid_newest_version(caplog):
    svc, store = _svc()
    good = svc.save_settings({}, source="cli")
    bad = store.insert_settings(doc={"schedules": {"ats_minutes": "often"}}, source="ui",
                                note=None, schema_version=1)

    def mutate(doc):
        doc.setdefault("sources", {})["lever"] = ["acme"]
        return "add lever:acme"

    with caplog.at_level("WARNING", logger="src.settings.service"):
        new = svc.update_settings(mutate, source="cli")
    warnings = [r for r in caplog.records if r.message == "settings_update_replaces_invalid_version"]
    assert len(warnings) == 1
    assert (warnings[0].invalid_version_id, warnings[0].version_id) == (bad, good)
    assert svc.snapshot().version_id == new
    assert svc.snapshot().degraded is None

    def mutate_again(doc):
        doc["sources"]["ashby"] = ["x"]
        return "add ashby:x"

    caplog.clear()
    with caplog.at_level("WARNING", logger="src.settings.service"):
        svc.update_settings(mutate_again, source="cli")  # nothing invalid left to replace
    assert not [r for r in caplog.records if r.message == "settings_update_replaces_invalid_version"]


def test_no_valid_version_counts_as_not_set_up(caplog):
    svc, store = _svc()
    store.insert_settings(doc={"schedules": {"ats_minutes": -1}}, source="ui", note=None, schema_version=1)
    with caplog.at_level("ERROR", logger="src.settings.service"):
        assert svc.snapshot() is None
    assert any(r.message == "settings_no_valid_version" for r in caplog.records)


def test_stored_row_with_unknown_timezone_degrades():
    svc, store = _svc()
    good = svc.save_settings({"schedules": {"ats_minutes": 7}}, source="cli")
    store.insert_settings(
        doc={"quiet_hours": {"timezone": "Mars/Olympus_Mons", "start": "22:00", "end": "07:00"}},
        source="ui", note=None, schema_version=1,
    )
    snap = svc.snapshot()
    assert snap.version_id == good
    assert snap.cfg.schedules.ats_minutes == 7
    assert snap.degraded is not None


def test_row_from_a_newer_schema_degrades_instead_of_crashing():
    svc, store = _svc()
    good = svc.save_settings({}, source="cli")
    store.insert_settings(doc={}, source="ui", note=None, schema_version=99)
    snap = svc.snapshot()
    assert snap.version_id == good
    assert "not supported" in snap.degraded.errors[0]["msg"]


def test_rows_are_migrated_on_read_and_saves_on_request():
    def v1_to_v2(doc):  # "cadence" became schedules.ats_minutes
        doc = dict(doc)
        return {**doc, "schedules": {"ats_minutes": doc.pop("cadence")}}

    store = SqliteSettingsStore(connect(":memory:"))
    store.insert_settings(doc={"cadence": 9}, source="import", note=None, schema_version=1)
    svc = ConfigService(store, env={}, migrations=[v1_to_v2], schema_version=2)
    assert svc.snapshot().cfg.schedules.ats_minutes == 9
    vid = svc.save_settings({"cadence": 4}, source="import", schema_version=1)
    row = store.get_settings_version(vid)
    assert row.schema_version == 2
    assert row.doc == {"schedules": {"ats_minutes": 4}}


# -- secrets ------------------------------------------------------------------------

def test_secret_names_cover_every_secrets_field():
    assert set(SECRET_NAMES) == set(Secrets.model_fields)
    assert secret_env_var("ops_ntfy_topic_url") == "JOB_AGG_OPS_NTFY_TOPIC_URL"


def test_secret_resolution_env_then_db_then_empty():
    svc, _ = _svc({"JOB_AGG_NTFY_TOPIC_URL": "https://env/ntfy", "JOB_AGG_DISCORD_WEBHOOK_URL": ""})
    svc.save_settings({}, source="cli")
    svc.set_secret("ntfy_topic_url", "https://db/ntfy")
    svc.set_secret("discord_webhook_url", "https://db/discord")
    secrets = svc.snapshot().cfg.secrets
    assert secrets.ntfy_topic_url == "https://env/ntfy"          # env wins
    assert secrets.discord_webhook_url == "https://db/discord"   # empty env counts as unset
    assert secrets.heartbeat_url == ""
    assert svc.secret_source("ntfy_topic_url") == "env"
    assert svc.secret_source("discord_webhook_url") == "stored"
    assert svc.secret_source("heartbeat_url") == "unset"


def test_secret_changes_rebuild_the_snapshot():
    svc, _ = _svc()
    svc.save_settings({}, source="cli")
    assert svc.snapshot().cfg.secrets.heartbeat_url == ""
    svc.set_secret("heartbeat_url", "https://hc/1")
    assert svc.snapshot().cfg.secrets.heartbeat_url == "https://hc/1"
    assert svc.clear_secret("heartbeat_url") is True
    assert svc.snapshot().cfg.secrets.heartbeat_url == ""


def test_set_secret_rejects_unknown_names_and_empty_values():
    svc, _ = _svc()
    with pytest.raises(ValueError, match="unknown secret"):
        svc.set_secret("password", "x")
    with pytest.raises(ValueError, match="must not be empty"):
        svc.set_secret("heartbeat_url", "")


def test_ollama_host_env_overrides_the_setting_without_persisting():
    svc, store = _svc({"JOB_AGG_OLLAMA_HOST": "https://ollama.com"})
    svc.save_settings({"relevance": {"ollama_host": "http://box:11434"}}, source="cli")
    assert svc.snapshot().cfg.relevance.ollama_host == "https://ollama.com"
    assert store.latest_settings().doc == {"relevance": {"ollama_host": "http://box:11434"}}


def test_empty_ollama_host_env_is_ignored():
    svc, _ = _svc({"JOB_AGG_OLLAMA_HOST": ""})
    svc.save_settings({}, source="cli")
    assert svc.snapshot().cfg.relevance.ollama_host == "http://ollama:11434"


def test_ollama_host_reports_the_effective_value_and_its_origin():
    not_set_up, _ = _svc()
    assert not_set_up.ollama_host() == ("http://ollama:11434", "default")
    env_before_setup, _ = _svc({"JOB_AGG_OLLAMA_HOST": "https://ollama.com"})
    assert env_before_setup.ollama_host() == ("https://ollama.com", "env")

    svc, _ = _svc({"JOB_AGG_OLLAMA_HOST": ""})
    svc.save_settings({"relevance": {"ollama_host": "http://box:11434"}}, source="cli")
    assert svc.ollama_host() == ("http://box:11434", "settings")
    svc.save_settings({}, source="cli")
    assert svc.ollama_host() == ("http://ollama:11434", "settings")  # in effect via settings

    overridden, _ = _svc({"JOB_AGG_OLLAMA_HOST": "https://ollama.com"})
    overridden.save_settings({"relevance": {"ollama_host": "http://box:11434"}}, source="cli")
    assert overridden.ollama_host() == ("https://ollama.com", "env")
