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
    for name in ("docker-compose.yml", "README.md", "GETTING_STARTED.md",
                 "SECURITY.md", "TROUBLESHOOTING.md"):
        assert "no authentication" not in (REPO / name).read_text().lower(), name


def test_env_example_documents_forwarded_allow_ips():
    assert "#FORWARDED_ALLOW_IPS=" in (REPO / ".env.example").read_text()


def _dockerfile() -> str:
    return (REPO / "Dockerfile").read_text()


def test_dockerfile_has_a_slim_default_and_a_headless_variant():
    text = _dockerfile()
    assert "AS runtime" in text
    assert "FROM runtime AS headless" in text
    # Chromium belongs only to the variant: the base must not install it.
    base, _, variant = text.partition("FROM runtime AS headless")
    assert "playwright install" not in base
    assert "playwright install" in variant


def test_base_image_omits_the_headless_extra():
    base, _, _ = _dockerfile().partition("FROM runtime AS headless")
    assert '"/app[web,render]"' in base
    assert "headless]" not in base


def test_dockerfile_does_not_ship_personal_resume_files():
    """resume/ holds no runtime input (packs live in src/tailor/render/templates),
    and .dockerignore does not exclude content.json or evidence.json, so copying
    the directory bakes a developer's own resume into the image."""
    assert "COPY resume/" not in _dockerfile()


def test_container_drops_privileges():
    text = _dockerfile()
    assert "docker-entrypoint.sh" in text
    assert "ENTRYPOINT" in text
    entrypoint = (REPO / "docker-entrypoint.sh").read_text()
    assert "setpriv" in entrypoint
