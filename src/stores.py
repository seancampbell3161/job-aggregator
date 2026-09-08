# src/stores.py
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Stores:
    seen: Any
    source_state: Any
    discovered: Any
    health: Any
    # Local cycle-telemetry sink (SQLite only). None on DynamoDB, where the ops
    # page reads cycle stats from CloudWatch Logs instead.
    events: Any = None
    # Audit + ops-alert stores (SQLite only; None on DynamoDB, where the audit
    # UI shows 'unavailable' and ops alerts are poller-local anyway).
    rejected: Any = None
    alert_state: Any = None
    boards: Any = None
    # Coach run history (SQLite only; None on DynamoDB → /coach reports unavailable).
    coach: Any = None
    # Résumé-builder settings singleton (SQLite only; None on DynamoDB).
    builder: Any = None


def _region() -> str:
    return os.environ.get("AWS_REGION", "us-east-1")


def build_stores(cfg: Any = None) -> Stores:
    """Construct the four stores for the active backend. `JOB_AGG_BACKEND`
    selects sqlite (default) or dynamodb. cfg is accepted for forward
    compatibility but unused today (table/path names come from env)."""
    backend = os.environ.get("JOB_AGG_BACKEND", "sqlite")
    if backend == "sqlite":
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
    if backend == "dynamodb":
        from src.state import (
            ConnectorHealthStore,
            DiscoveredSlugsStore,
            SeenJobsStore,
            SourceStateStore,
        )
        region = _region()
        return Stores(
            seen=SeenJobsStore(
                table_name=os.environ.get("JOB_AGG_SEEN_JOBS_TABLE", "seen_jobs"),
                region=region),
            source_state=SourceStateStore(
                table_name=os.environ.get("JOB_AGG_SOURCE_STATE_TABLE", "source_state"),
                region=region),
            discovered=DiscoveredSlugsStore(
                table_name=os.environ.get("JOB_AGG_DISCOVERED_SLUGS_TABLE", "discovered_slugs"),
                region=region),
            health=ConnectorHealthStore(
                table_name=os.environ.get("JOB_AGG_CONNECTOR_HEALTH_TABLE", "connector_health"),
                region=region),
        )
    raise ValueError(f"unknown JOB_AGG_BACKEND: {backend!r} (expected 'sqlite' or 'dynamodb')")
