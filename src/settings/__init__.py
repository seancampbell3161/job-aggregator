"""SQLite-backed settings: versioned settings documents, documents (profile,
résumé, tailoring data, apply-kit facts), and secrets. See service.py."""
from __future__ import annotations


def open_service(path: str | None = None):
    """A ConfigService over its own connection to the app DB
    (JOB_AGG_SQLITE_PATH unless ``path`` is given)."""
    from src.settings.service import ConfigService
    from src.settings.store import SqliteSettingsStore
    from src.sqlite_db import connect

    return ConfigService(SqliteSettingsStore(connect(path)))
