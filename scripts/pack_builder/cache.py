"""SQLite cache of verification results keyed by candidate identity.

Autocommit: every put is durable at once, so an interrupted build loses only
the checks in flight, and a re-run within --max-age-days skips what is fresh.
Deferred results (throttled/timeouts) are always re-checked."""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timedelta, timezone
from pathlib import Path

STATUSES = ("live", "dead", "deferred")


@dataclass(frozen=True)
class CheckResult:
    status: str
    postings: int = 0          # fetched per poll: the pack's budget unit
    us_postings: int = 0
    eu_postings: int = 0
    company: str | None = None
    connector_name: str | None = None
    identity: dict | None = None   # as verified (eightfold resolves domain/flavor)
    reason: str | None = None      # why a dead/deferred check failed; None when live


_FIELDS = frozenset(f.name for f in fields(CheckResult))


def _load(data: str) -> CheckResult:
    """A cached row, ignoring fields an older or newer builder wrote that
    this CheckResult doesn't have."""
    return CheckResult(**{k: v for k, v in json.loads(data).items() if k in _FIELDS})


class BuildCache:
    def __init__(self, path: Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), isolation_level=None)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS checks (key TEXT PRIMARY KEY, family TEXT NOT NULL,"
            " data TEXT NOT NULL, checked_at TEXT NOT NULL)")

    def put(self, key: str, family: str, result: CheckResult, *,
            now: datetime | None = None) -> None:
        at = (now or datetime.now(timezone.utc)).isoformat()
        self._conn.execute("INSERT OR REPLACE INTO checks VALUES (?, ?, ?, ?)",
                           (key, family, json.dumps(asdict(result)), at))

    def get(self, key: str) -> tuple[CheckResult, datetime] | None:
        row = self._conn.execute("SELECT data, checked_at FROM checks WHERE key = ?",
                                 (key,)).fetchone()
        if row is None:
            return None
        return _load(row[0]), datetime.fromisoformat(row[1])

    def needs_check(self, key: str, *, max_age_days: int,
                    now: datetime | None = None) -> bool:
        hit = self.get(key)
        if hit is None:
            return True
        result, at = hit
        now = now or datetime.now(timezone.utc)
        return result.status == "deferred" or now - at > timedelta(days=max_age_days)

    def results(self, keys: Iterable[str]) -> dict[str, tuple[str, CheckResult]]:
        wanted = set(keys)
        out: dict[str, tuple[str, CheckResult]] = {}
        for key, family, data in self._conn.execute("SELECT key, family, data FROM checks"):
            if key in wanted:
                out[key] = (family, _load(data))
        return out

    def all_results(self) -> dict[str, tuple[str, CheckResult]]:
        """Every cached row, whatever its age or status."""
        return {key: (family, _load(data)) for key, family, data
                in self._conn.execute("SELECT key, family, data FROM checks")}

    def close(self) -> None:
        self._conn.close()
