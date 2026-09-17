"""SQLite persistence for settings versions, documents, and secrets.

Persistence only — no validation (ConfigService validates before calling in).
settings_versions and documents are append-only; the newest row (per kind for
documents) is current. Every write runs inside BEGIN IMMEDIATE and bumps
config_generation.n in the same transaction, so a process polling the
generation never sees a write without its signal, or the reverse."""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterator, Sequence

from src.settings.errors import StaleWrite


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class SettingsRow:
    id: int
    created_at: str
    source: str
    note: str | None
    schema_version: int
    doc: dict


@dataclass(frozen=True)
class DocumentRow:
    id: int
    kind: str
    created_at: str
    source: str
    body: str


@dataclass(frozen=True)
class NewSettings:
    doc: dict
    note: str | None
    schema_version: int
    base_version_id: int | None = None


@dataclass(frozen=True)
class NewDocument:
    kind: str
    body: str
    base_document_id: int | None = None


def _settings_row(row: sqlite3.Row) -> SettingsRow:
    return SettingsRow(
        id=int(row["id"]), created_at=row["created_at"], source=row["source"],
        note=row["note"], schema_version=int(row["schema_version"]),
        doc=json.loads(row["doc"]),
    )


def _document_row(row: sqlite3.Row) -> DocumentRow:
    return DocumentRow(
        id=int(row["id"]), kind=row["kind"], created_at=row["created_at"],
        source=row["source"], body=row["body"],
    )


class SqliteSettingsStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        # One connection can be shared across threads (APScheduler workers,
        # FastAPI's threadpool): serialize write transactions on it.
        self._lock = threading.RLock()

    # -- write plumbing ------------------------------------------------------

    @contextmanager
    def _write(self) -> Iterator[None]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield
                self._bump_generation()
                self._conn.execute("COMMIT")
            except BaseException:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise

    def _bump_generation(self) -> None:
        self._conn.execute(
            "INSERT INTO config_generation (id, n) VALUES (1, 1) "
            "ON CONFLICT(id) DO UPDATE SET n = n + 1"
        )

    # -- generation ------------------------------------------------------------

    def generation(self) -> int:
        row = self._conn.execute("SELECT n FROM config_generation WHERE id = 1").fetchone()
        return int(row["n"]) if row else 0

    # -- settings versions -------------------------------------------------------

    def latest_settings(self) -> SettingsRow | None:
        row = self._conn.execute(
            "SELECT * FROM settings_versions ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return _settings_row(row) if row else None

    def settings_versions(self, limit: int | None = 50) -> list[SettingsRow]:
        if limit is None:
            rows = self._conn.execute("SELECT * FROM settings_versions ORDER BY id DESC")
        else:
            rows = self._conn.execute(
                "SELECT * FROM settings_versions ORDER BY id DESC LIMIT ?", (limit,)
            )
        return [_settings_row(r) for r in rows]

    def get_settings_version(self, version_id: int) -> SettingsRow | None:
        row = self._conn.execute(
            "SELECT * FROM settings_versions WHERE id = ?", (version_id,)
        ).fetchone()
        return _settings_row(row) if row else None

    def insert_settings(
        self, *, doc: dict, source: str, note: str | None, schema_version: int,
        base_version_id: int | None = None,
    ) -> int:
        settings_id, _ = self.insert_bundle(
            source=source,
            settings=NewSettings(doc=doc, note=note, schema_version=schema_version,
                                 base_version_id=base_version_id),
        )
        assert settings_id is not None
        return settings_id

    # -- documents -----------------------------------------------------------------

    def latest_document(self, kind: str) -> DocumentRow | None:
        row = self._conn.execute(
            "SELECT * FROM documents WHERE kind = ? ORDER BY id DESC LIMIT 1", (kind,)
        ).fetchone()
        return _document_row(row) if row else None

    def insert_document(
        self, *, kind: str, body: str, source: str, base_document_id: int | None = None,
    ) -> int:
        _, ids = self.insert_bundle(
            source=source,
            documents=[NewDocument(kind=kind, body=body, base_document_id=base_document_id)],
        )
        return ids[0]

    # -- bundle (one transaction, one generation bump) -----------------------------

    def insert_bundle(
        self, *, source: str, settings: NewSettings | None = None,
        documents: Sequence[NewDocument] = (),
    ) -> tuple[int | None, list[int]]:
        now = _now_iso()
        with self._write():
            settings_id: int | None = None
            if settings is not None:
                self._check_base_version(settings.base_version_id)
                cur = self._conn.execute(
                    "INSERT INTO settings_versions (created_at, source, note, schema_version, doc) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (now, source, settings.note, settings.schema_version, json.dumps(settings.doc)),
                )
                settings_id = int(cur.lastrowid)
            doc_ids: list[int] = []
            for d in documents:
                self._check_base_document(d.kind, d.base_document_id)
                cur = self._conn.execute(
                    "INSERT INTO documents (kind, created_at, source, body) VALUES (?, ?, ?, ?)",
                    (d.kind, now, source, d.body),
                )
                doc_ids.append(int(cur.lastrowid))
        return settings_id, doc_ids

    def _check_base_version(self, base: int | None) -> None:
        if base is None:
            return
        latest = self._conn.execute("SELECT MAX(id) AS id FROM settings_versions").fetchone()["id"]
        if latest != base:
            raise StaleWrite(f"settings base version {base} is stale (latest is {latest})")

    def _check_base_document(self, kind: str, base: int | None) -> None:
        if base is None:
            return
        latest = self._conn.execute(
            "SELECT MAX(id) AS id FROM documents WHERE kind = ?", (kind,)
        ).fetchone()["id"]
        if latest != base:
            raise StaleWrite(f"{kind} base document {base} is stale (latest is {latest})")

    # -- secrets -------------------------------------------------------------------

    def get_secret(self, name: str) -> str | None:
        row = self._conn.execute("SELECT value FROM secrets WHERE name = ?", (name,)).fetchone()
        return row["value"] if row else None

    def put_secret(self, name: str, value: str) -> None:
        with self._write():
            self._conn.execute(
                "INSERT INTO secrets (name, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET value = excluded.value, "
                "updated_at = excluded.updated_at",
                (name, value, _now_iso()),
            )

    def put_secret_if_absent(self, name: str, value: str) -> bool:
        with self._write():
            cur = self._conn.execute(
                "INSERT INTO secrets (name, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(name) DO NOTHING",
                (name, value, _now_iso()),
            )
            inserted = cur.rowcount == 1
        return inserted

    def delete_secret(self, name: str) -> bool:
        with self._write():
            cur = self._conn.execute("DELETE FROM secrets WHERE name = ?", (name,))
            deleted = cur.rowcount == 1
        return deleted

    def stored_secret_names(self) -> set[str]:
        return {r["name"] for r in self._conn.execute("SELECT name FROM secrets")}

    def all_secrets(self) -> dict[str, str]:
        return {r["name"]: r["value"] for r in self._conn.execute("SELECT name, value FROM secrets")}
