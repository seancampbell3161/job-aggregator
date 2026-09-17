# src/stores.py
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

log = logging.getLogger(__name__)

if TYPE_CHECKING:
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


def build_stores(cfg: Any = None) -> Stores:
    """Construct every store over one shared SQLite connection
    (JOB_AGG_SQLITE_PATH). cfg is accepted for forward compatibility but
    unused today."""
    backend = os.environ.get("JOB_AGG_BACKEND")
    if backend and backend != "sqlite":
        log.warning("legacy_backend_env_ignored", extra={"value": backend})
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
        settings=SqliteSettingsStore(conn),
    )
