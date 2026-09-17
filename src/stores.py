# src/stores.py
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from src.auth.store import SqliteAuthStore
    from src.settings.store import SqliteSettingsStore
    from src.state_sqlite import (
        SqliteBuilderSettingsStore,
        SqliteCoachRunsStore,
        SqliteConnectorHealthStore,
        SqliteDiscoveredBoardsStore,
        SqliteDiscoveredSlugsStore,
        SqliteOpsAlertStateStore,
        SqlitePipelineEventsStore,
        SqliteRejectedPostingsStore,
        SqliteSeenJobsStore,
        SqliteSourceStateStore,
    )


@dataclass(frozen=True)
class Stores:
    seen: SqliteSeenJobsStore
    source_state: SqliteSourceStateStore
    discovered: SqliteDiscoveredSlugsStore
    health: SqliteConnectorHealthStore
    events: SqlitePipelineEventsStore
    rejected: SqliteRejectedPostingsStore
    alert_state: SqliteOpsAlertStateStore
    boards: SqliteDiscoveredBoardsStore
    coach: SqliteCoachRunsStore
    builder: SqliteBuilderSettingsStore
    settings: SqliteSettingsStore
    auth: SqliteAuthStore


def build_stores(cfg: Any = None) -> Stores:
    """Construct every store over one shared SQLite connection
    (JOB_AGG_SQLITE_PATH), except `settings` (see below). cfg is accepted for
    forward compatibility but unused today."""
    backend = os.environ.get("JOB_AGG_BACKEND")
    if backend and backend != "sqlite":
        log.warning("legacy_backend_env_ignored", extra={"value": backend})
    from src.auth.store import SqliteAuthStore
    from src.settings.store import SqliteSettingsStore
    from src.sqlite_db import connect
    from src.state_sqlite import (
        SqliteBuilderSettingsStore,
        SqliteConnectorHealthStore,
        SqliteCoachRunsStore,
        SqliteDiscoveredBoardsStore,
        SqliteDiscoveredSlugsStore,
        SqliteOpsAlertStateStore,
        SqlitePipelineEventsStore,
        SqliteRejectedPostingsStore,
        SqliteSeenJobsStore,
        SqliteSourceStateStore,
    )
    conn = connect()
    return Stores(
        seen=SqliteSeenJobsStore(conn),
        source_state=SqliteSourceStateStore(conn),
        discovered=SqliteDiscoveredSlugsStore(conn),
        health=SqliteConnectorHealthStore(conn),
        events=SqlitePipelineEventsStore(conn),
        rejected=SqliteRejectedPostingsStore(conn),
        alert_state=SqliteOpsAlertStateStore(conn),
        boards=SqliteDiscoveredBoardsStore(conn),
        coach=SqliteCoachRunsStore(conn),
        builder=SqliteBuilderSettingsStore(conn),
        # Its own connection: SqliteSettingsStore.read()/_write() BEGIN their
        # own transactions, which would collide ("cannot start a transaction
        # within a transaction") with any other store's BEGIN IMMEDIATE on a
        # shared connection.
        settings=SqliteSettingsStore(connect()),
        # Also its own connection, for the same reason: change-password and
        # the CLI reset run BEGIN IMMEDIATE transactions.
        auth=SqliteAuthStore(connect()),
    )
