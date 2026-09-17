"""Guards on the deploy files: settings live in ./data, nothing else is mounted."""
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent


def _compose() -> dict:
    return yaml.safe_load((REPO / "docker-compose.yml").read_text())


def test_app_services_mount_only_the_data_dir():
    services = _compose()["services"]
    for name in ("poller", "web"):
        assert services[name]["volumes"] == ["./data:/data"], name


def test_env_file_is_optional_and_paths_point_at_data():
    services = _compose()["services"]
    for name in ("poller", "web"):
        svc = services[name]
        assert svc["env_file"] == [{"path": ".env", "required": False}], name
        env = svc["environment"]
        assert env["JOB_AGG_SQLITE_PATH"] == "/data/job_aggregator.db"
        assert env["JOB_AGG_TAILORED_DIR"] == "/data/tailored"
        assert env["JOB_AGG_TEMPLATES_DIR"] == "/data/templates"
        assert "JOB_AGG_OLLAMA_HOST" not in env  # relevance.ollama_host is the default


def test_env_example_sets_nothing_by_default():
    active = [
        line for line in (REPO / ".env.example").read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert active == []


def test_image_defaults_templates_into_data():
    assert "JOB_AGG_TEMPLATES_DIR=/data/templates" in (REPO / "Dockerfile").read_text()


def test_docs_no_longer_say_the_ui_has_no_authentication():
    for name in ("docker-compose.yml", "README.md", "GETTING_STARTED.md"):
        assert "no authentication" not in (REPO / name).read_text().lower(), name


def test_env_example_documents_forwarded_allow_ips():
    assert "#FORWARDED_ALLOW_IPS=" in (REPO / ".env.example").read_text()
