# src/stores.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Stores:
    seen: Any
    source_state: Any
    discovered: Any
    health: Any
    events: Any = None
    rejected: Any = None
    alert_state: Any = None
    boards: Any = None
    coach: Any = None
    builder: Any = None


def build_stores(cfg: Any = None) -> Stores:
    """Construct every store over one shared SQLite connection
    (JOB_AGG_SQLITE_PATH). cfg is accepted for forward compatibility but
    unused today."""
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
    )
