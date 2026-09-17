"""python -m src.settings — command wiring, exit codes, and secret hygiene."""
import io
import os
import subprocess
import sys
from pathlib import Path

import pytest

from src.settings.cli import main
from tests.settings_helpers import make_service

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _templates_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("JOB_AGG_TEMPLATES_DIR", str(tmp_path / "templates"))


def test_status_when_not_configured(capsys):
    assert main(["status"], service=make_service()) == 0
    out = capsys.readouterr().out
    assert "not configured" in out
    assert "ntfy_topic_url" in out and "unset" in out


def test_import_then_history_and_status(tmp_path, capsys):
    (tmp_path / "config.yaml").write_text("schedules: {slow_minutes: 30}\nbogus: 1\n")
    (tmp_path / "profile.md").write_text("# me")
    svc = make_service()
    assert main(["import", str(tmp_path)], service=svc) == 0
    captured = capsys.readouterr()
    assert "imported settings version" in captured.out
    assert "bogus: unknown key, ignored" in captured.err
    assert main(["history"], service=svc) == 0
    out = capsys.readouterr().out
    assert "import" in out and "*" in out
    assert main(["status"], service=svc) == 0
    assert "configured (settings version" in capsys.readouterr().out


def test_invalid_import_exits_1_and_writes_nothing(tmp_path, capsys):
    (tmp_path / "config.yaml").write_text("schedules: {ats_minutes: 0}\n")
    svc = make_service()
    assert main(["import", str(tmp_path)], service=svc) == 1
    assert "schedules.ats_minutes" in capsys.readouterr().err
    assert svc.snapshot() is None


def test_missing_config_exits_1(tmp_path, capsys):
    assert main(["import", str(tmp_path)], service=make_service()) == 1
    assert "not found" in capsys.readouterr().err


def test_export(tmp_path):
    svc = make_service({"schedules": {"slow_minutes": 30}}, documents={"profile": "# me"})
    assert main(["export", str(tmp_path / "out")], service=svc) == 0
    assert (tmp_path / "out" / "profile.md").read_text() == "# me"


def test_export_when_not_configured_exits_1(tmp_path):
    assert main(["export", str(tmp_path)], service=make_service()) == 1


def test_set_secret_reads_stdin_never_argv(monkeypatch, capsys):
    svc = make_service({})
    monkeypatch.setattr(sys, "stdin", io.StringIO("https://ntfy.sh/topic\n"))
    assert main(["set-secret", "ntfy_topic_url"], service=svc) == 0
    assert svc.snapshot().cfg.secrets.ntfy_topic_url == "https://ntfy.sh/topic"
    captured = capsys.readouterr()
    assert "https://ntfy.sh/topic" not in captured.out + captured.err
    with pytest.raises(SystemExit) as exc:
        main(["set-secret", "ntfy_topic_url", "https://ntfy.sh/on-argv"], service=svc)
    assert exc.value.code == 2


def test_set_secret_empty_value_exits_1(monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO("\n"))
    assert main(["set-secret", "heartbeat_url"], service=make_service({})) == 1


def test_set_secret_unknown_name_is_a_usage_error():
    with pytest.raises(SystemExit) as exc:
        main(["set-secret", "password"], service=make_service({}))
    assert exc.value.code == 2


def test_set_secret_notes_env_precedence(monkeypatch, capsys):
    svc = make_service({}, env={"JOB_AGG_HEARTBEAT_URL": "https://env"})
    monkeypatch.setattr(sys, "stdin", io.StringIO("https://db\n"))
    assert main(["set-secret", "heartbeat_url"], service=svc) == 0
    assert "JOB_AGG_HEARTBEAT_URL" in capsys.readouterr().err


def test_clear_secret():
    svc = make_service({}, secrets={"heartbeat_url": "https://hc/1"})
    assert main(["clear-secret", "heartbeat_url"], service=svc) == 0
    assert svc.secret_source("heartbeat_url") == "unset"


def test_import_env_secrets_prints_names_not_values(capsys):
    svc = make_service({}, env={"JOB_AGG_GMAIL_ADDRESS": "me@example.com"})
    assert main(["import-env-secrets"], service=svc) == 0
    out = capsys.readouterr().out
    assert "gmail_address" in out
    assert "me@example.com" not in out
    assert svc.secret_source("gmail_address") == "env"  # env still wins while set
    assert "JOB_AGG_OLLAMA_HOST" not in out  # the note only appears when it's set


def test_import_env_secrets_notes_that_the_ollama_host_is_not_copied(capsys):
    svc = make_service({}, env={"JOB_AGG_OLLAMA_HOST": "https://ollama.com"})
    assert main(["import-env-secrets"], service=svc) == 0
    out = capsys.readouterr().out
    assert ("JOB_AGG_OLLAMA_HOST is not a secret and is not copied — keep it in .env, "
            "or set relevance.ollama_host in config.yaml and import.") in out
    assert svc.ollama_host() == ("https://ollama.com", "env")


def test_status_shows_the_ollama_host_and_its_origin(capsys):
    assert main(["status"], service=make_service()) == 0
    assert "ollama host: http://ollama:11434 (default)" in capsys.readouterr().out
    assert main(["status"], service=make_service(env={"JOB_AGG_OLLAMA_HOST": "https://ollama.com"})) == 0
    assert "ollama host: https://ollama.com (env JOB_AGG_OLLAMA_HOST)" in capsys.readouterr().out
    svc = make_service({"relevance": {"ollama_host": "http://gpu-box:11434"}})
    assert main(["status"], service=svc) == 0
    assert "ollama host: http://gpu-box:11434 (settings)" in capsys.readouterr().out


def test_history_and_status_survive_a_corrupt_settings_row(capsys):
    svc = make_service({"schedules": {"slow_minutes": 30}})
    store = svc._store
    with store._write():
        store._conn.execute(
            "INSERT INTO settings_versions (created_at, source, note, schema_version, doc) "
            "VALUES ('2026-09-17T00:00:00+00:00', 'ui', 'corrupt', 1, 'not json')"
        )
    assert main(["history"], service=svc) == 0
    out = capsys.readouterr().out
    assert "corrupt" in out and "test fixture" in out
    assert main(["status"], service=svc) == 0
    assert "is not valid JSON" in capsys.readouterr().out


def test_restore(capsys):
    svc = make_service({"schedules": {"slow_minutes": 30}})
    first = svc.versions()[0].id
    svc.save_settings({}, source="cli")
    assert main(["restore", str(first)], service=svc) == 0
    assert svc.snapshot().cfg.schedules.slow_minutes == 30
    assert main(["restore", "999"], service=svc) == 1


def test_add_source_is_idempotent(capsys):
    svc = make_service({})
    assert main(["add-source", "greenhouse", "stripe"], service=svc) == 0
    assert main(["add-source", "greenhouse", "stripe"], service=svc) == 0
    assert "already" in capsys.readouterr().out
    assert svc.snapshot().cfg.sources.greenhouse == ["stripe"]
    assert len(svc.versions()) == 2  # fixture version + one add


def test_add_source_before_setup_exits_1(capsys):
    assert main(["add-source", "lever", "acme"], service=make_service()) == 1
    assert "import" in capsys.readouterr().err


def test_add_source_rejects_structured_families():
    with pytest.raises(SystemExit) as exc:
        main(["add-source", "workday", "x"], service=make_service({}))
    assert exc.value.code == 2


def test_module_entrypoint_opens_the_app_db(tmp_path):
    db = tmp_path / "cli.db"
    env = {**os.environ, "JOB_AGG_SQLITE_PATH": str(db)}
    result = subprocess.run(
        [sys.executable, "-m", "src.settings", "status"],
        capture_output=True, text=True, env=env, cwd=REPO,
    )
    assert result.returncode == 0, result.stderr
    assert "not configured" in result.stdout
    assert db.exists()
