"""The web UI login: one admin password (argon2id) and server-side sessions,
stored in the app database. See service.py."""


def open_auth_service(path: str | None = None):
    """An AuthService over its own connection to the app DB
    (JOB_AGG_SQLITE_PATH unless ``path`` is given)."""
    from src.auth.service import AuthService
    from src.auth.store import SqliteAuthStore
    from src.sqlite_db import connect

    return AuthService(SqliteAuthStore(connect(path)))
