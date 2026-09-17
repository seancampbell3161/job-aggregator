"""Settings fixtures — the replacement for writing config.yaml and pointing
JOB_AGG_CONFIG_PATH at it."""
from __future__ import annotations

import os
import sqlite3
from typing import Mapping

import yaml

from src.settings.service import ConfigService
from src.settings.store import SqliteSettingsStore
from src.sqlite_db import connect


def _as_doc(doc: dict | str) -> dict:
    if isinstance(doc, str):
        return yaml.safe_load(doc) or {}
    return doc


def make_service(
    doc: dict | str | None = None,
    *,
    documents: Mapping[str, str] | None = None,
    secrets: Mapping[str, str] | None = None,
    conn: sqlite3.Connection | None = None,
    env: Mapping[str, str] | None = None,
) -> ConfigService:
    """A ConfigService over ``conn`` (default: a fresh in-memory DB).

    ``doc`` (a dict or YAML text) and ``documents`` are saved as one bundle.
    ``doc=None`` leaves the instance not set up (documents are then saved on
    their own). ``env`` defaults to {} so the host environment never leaks
    into a test."""
    service = ConfigService(
        SqliteSettingsStore(conn if conn is not None else connect(":memory:")),
        env={} if env is None else env,
    )
    if doc is not None:
        service.save_bundle(_as_doc(doc), dict(documents or {}), source="cli", note="test fixture")
    else:
        for kind, body in (documents or {}).items():
            service.save_document(kind, body, source="cli")
    for name, value in (secrets or {}).items():
        service.set_secret(name, value)
    return service


# The score thresholds create_app used to default to; web tests assert band
# classes against them.
WEB_TEST_SETTINGS = {"relevance": {"score_high": 7, "score_low": 4}}


def configured_stores(conn: sqlite3.Connection, doc: dict | None = None, *,
                      documents: Mapping[str, str] | None = None):
    """tests.sqlite_helpers.sqlite_stores plus a saved settings version, so the
    web app treats the instance as set up (otherwise every page redirects to
    /setup)."""
    from tests.sqlite_helpers import sqlite_stores

    stores = sqlite_stores(conn)
    ConfigService(stores.settings, env={}).save_bundle(
        WEB_TEST_SETTINGS if doc is None else doc, dict(documents or {}),
        source="cli", note="test fixture",
    )
    return stores


def seed_settings(
    doc: dict | str | None = None,
    *,
    documents: Mapping[str, str] | None = None,
    secrets: Mapping[str, str] | None = None,
) -> ConfigService:
    """Save settings into the per-test DB at JOB_AGG_SQLITE_PATH (isolated by
    tests/conftest.py) for code that opens its own stores: handler._run,
    scheduler jobs, scripts. Call it AFTER any setenv of JOB_AGG_SQLITE_PATH.
    The returned service reads os.environ, like production code does."""
    return make_service(
        {} if doc is None else doc, documents=documents, secrets=secrets,
        conn=connect(), env=os.environ,
    )
