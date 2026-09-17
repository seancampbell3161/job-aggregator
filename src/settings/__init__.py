"""SQLite-backed settings: versioned settings documents, documents (profile,
résumé, tailoring data, apply-kit facts), and secrets. See service.py."""
from __future__ import annotations

# Printed after a database-only settings change (add-source, the --merge
# scripts, seed_companies). Import replaces the whole settings document, so a
# later import from files that predate the change would refuse to run.
EXPORT_TIP = (
    "Tip: run `python -m src.settings export DIR` before editing settings files, "
    "so this change isn't lost."
)

# Printed to stderr by the host-side scripts before they write settings.
# SQLite locks don't cross the Docker Desktop bind mount between the host and
# the containers (verified on macOS), so a host write while the poller or web
# container writes can corrupt the database.
HOST_WRITE_WARNING = (
    "Writing to the settings database from the host: stop the poller and web "
    "containers first (docker compose stop poller web)."
)


def open_service(path: str | None = None):
    """A ConfigService over its own connection to the app DB
    (JOB_AGG_SQLITE_PATH unless ``path`` is given)."""
    from src.settings.service import ConfigService
    from src.settings.store import SqliteSettingsStore
    from src.sqlite_db import connect

    return ConfigService(SqliteSettingsStore(connect(path)))
