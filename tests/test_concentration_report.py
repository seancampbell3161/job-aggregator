import os
from pathlib import Path

from scripts.concentration_report import _blocked_companies
from src.sqlite_db import connect
from tests.settings_helpers import seed_settings


def test_blocked_companies_come_from_settings():
    seed_settings({"filters": {"blocked_companies": ["Microsoft", "career launch"]}})
    db = Path(os.environ["JOB_AGG_SQLITE_PATH"])
    assert _blocked_companies(db) == [["microsoft"], ["career", "launch"]]


def test_blocked_companies_empty_when_not_set_up(tmp_path):
    db = tmp_path / "empty.db"
    connect(str(db))
    assert _blocked_companies(db) == []


def test_blocked_companies_empty_when_the_db_is_missing(tmp_path):
    assert _blocked_companies(tmp_path / "missing.db") == []
