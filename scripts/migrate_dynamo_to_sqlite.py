"""One-shot migration: copy the four DynamoDB tables into a local SQLite DB.

Run once on a machine with read access to the AWS account:

    JOB_AGG_SQLITE_PATH=./data/job_aggregator.db \
    python -m scripts.migrate_dynamo_to_sqlite

Idempotent — re-running upserts. The SQLite stores share the destination
connection's schema (see src/sqlite_db.connect)."""

from __future__ import annotations

import json
import os
import sqlite3
from decimal import Decimal
from typing import Any

import boto3

from src.sqlite_db import connect


def _scan(table) -> list[dict]:
    items: list[dict] = []
    kwargs: dict = {}
    while True:
        resp = table.scan(**kwargs)
        items.extend(resp.get("Items", []))
        last = resp.get("LastEvaluatedKey")
        if not last:
            break
        kwargs["ExclusiveStartKey"] = last
    return items


def _plain(v: Any) -> Any:
    """Coerce DynamoDB Decimals to int/float and recurse into lists/dicts so the
    item is JSON-serializable for the SQLite `data` column."""
    if isinstance(v, Decimal):
        return int(v) if v == v.to_integral_value() else float(v)
    if isinstance(v, list):
        return [_plain(x) for x in v]
    if isinstance(v, dict):
        return {k: _plain(x) for k, x in v.items()}
    return v


def migrate(*, region: str, dst_conn: sqlite3.Connection) -> dict[str, int]:
    ddb = boto3.resource("dynamodb", region_name=region)
    counts: dict[str, int] = {}

    # seen_jobs — preserve the full item shape in the data column + mirror columns.
    seen_items = _scan(ddb.Table(os.environ.get("JOB_AGG_SEEN_JOBS_TABLE", "seen_jobs")))
    for raw in seen_items:
        item = _plain(raw)
        dst_conn.execute(
            "INSERT OR REPLACE INTO seen_jobs (job_id, first_seen, notified, ttl, score, title, data) "
            "VALUES (?,?,?,?,?,?,?)",
            (item["job_id"], item.get("first_seen"),
             1 if item.get("notified") else 0, item.get("ttl"),
             item.get("score"), item.get("title"), json.dumps(item)),
        )
    counts["seen_jobs"] = len(seen_items)

    # source_state
    ss = _scan(ddb.Table(os.environ.get("JOB_AGG_SOURCE_STATE_TABLE", "source_state")))
    for raw in ss:
        item = _plain(raw)
        data = {k: v for k, v in item.items() if k != "connector_name"}
        dst_conn.execute(
            "INSERT OR REPLACE INTO source_state (connector_name, data) VALUES (?, ?)",
            (item["connector_name"], json.dumps(data)),
        )
    counts["source_state"] = len(ss)

    # discovered_slugs
    ds = _scan(ddb.Table(os.environ.get("JOB_AGG_DISCOVERED_SLUGS_TABLE", "discovered_slugs")))
    for raw in ds:
        item = _plain(raw)
        dst_conn.execute(
            "INSERT OR REPLACE INTO discovered_slugs (connector_name, data) VALUES (?, ?)",
            (item["connector_name"], json.dumps(item)),
        )
    counts["discovered_slugs"] = len(ds)

    # connector_health (dedicated columns)
    ch = _scan(ddb.Table(os.environ.get("JOB_AGG_CONNECTOR_HEALTH_TABLE", "connector_health")))
    for raw in ch:
        item = _plain(raw)
        dst_conn.execute(
            "INSERT OR REPLACE INTO connector_health "
            "(connector_name, dead_streak, suppressed, updated_at) VALUES (?,?,?,?)",
            (item["connector_name"], int(item.get("dead_streak", 0) or 0),
             1 if item.get("suppressed") else 0, item.get("updated_at")),
        )
    counts["connector_health"] = len(ch)

    return counts


def main() -> int:
    region = os.environ.get("AWS_REGION", "us-east-1")
    conn = connect()  # JOB_AGG_SQLITE_PATH
    counts = migrate(region=region, dst_conn=conn)
    for table, n in counts.items():
        print(f"{table}: {n} rows")
    print(f"-> {os.environ.get('JOB_AGG_SQLITE_PATH', 'data/job_aggregator.db')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
