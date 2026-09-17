import os

import pytest

from scripts.tailor_smoke import _signing_config
from tests.settings_helpers import seed_settings


def test_signing_config_resolves_the_env_file_over_the_db(tmp_path, monkeypatch):
    monkeypatch.delenv("JOB_AGG_TAILOR_SIGNING_SECRET", raising=False)
    monkeypatch.delenv("JOB_AGG_TAILOR_ENDPOINT_URL", raising=False)
    seed_settings({}, secrets={"tailor_signing_secret": "db-secret",
                               "tailor_endpoint_url": "https://db.example/tailor"})
    env_file = tmp_path / ".env"
    env_file.write_text("JOB_AGG_TAILOR_ENDPOINT_URL=https://env.example/tailor\n")
    assert _signing_config(os.environ["JOB_AGG_SQLITE_PATH"], str(env_file)) == (
        "db-secret", "https://env.example/tailor",
    )


def test_signing_config_requires_setup(tmp_path):
    with pytest.raises(SystemExit, match="not set up"):
        _signing_config(str(tmp_path / "empty.db"), str(tmp_path / "no.env"))


def test_signing_config_requires_an_endpoint(tmp_path, monkeypatch):
    monkeypatch.delenv("JOB_AGG_TAILOR_ENDPOINT_URL", raising=False)
    seed_settings({}, secrets={"tailor_signing_secret": "s"})
    with pytest.raises(SystemExit, match="tailor_endpoint_url"):
        _signing_config(os.environ["JOB_AGG_SQLITE_PATH"], str(tmp_path / "no.env"))
