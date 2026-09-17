# src/state_sqlite.py
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from typing import Iterable

from src.models import ConnectorState, NormalizedPosting
from src.sanitize import sanitize_description
from src.state import (
    _KEPT_STATUSES,
    _TTL_DAYS,
    VALID_STATUSES,
    DiscoveredSlug,
    DiscoveredBoard,
    match_view,
    _row_from_item,
    _board_from_item,
    posting_display_fields,
)
from src.tailor.endpoint.jd import PostingJD


def _now_ts() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _ttl() -> int:
    return int((datetime.now(timezone.utc) + timedelta(days=_TTL_DAYS)).timestamp())


class SqliteSeenJobsStore:
    """SQLite twin of state.SeenJobsStore. Stores the full DynamoDB-shaped item
    dict as JSON in `data`, plus mirror columns for the fields used in queries,
    so read methods share state.match_view for view shaping.
    Expired rows (ttl < now) are hidden on read and removed by prune_expired."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._write_lock = threading.Lock()

    def _write(self, item: dict, *, ignore: bool) -> int:
        verb = "INSERT OR IGNORE" if ignore else "INSERT OR REPLACE"
        cur = self._conn.execute(
            f"{verb} INTO seen_jobs (job_id, first_seen, notified, ttl, score, title, data) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                item["job_id"], item.get("first_seen"),
                1 if item.get("notified") else 0,
                item.get("ttl"), item.get("score"), item.get("title"),
                json.dumps(item),
            ),
        )
        return cur.rowcount

    def diff_new(self, job_ids: Iterable[str]) -> list[str]:
        candidates = list(dict.fromkeys(job_ids))
        if not candidates:
            return []
        now = _now_ts()
        seen: set[str] = set()
        CHUNK = 500
        for i in range(0, len(candidates), CHUNK):
            chunk = candidates[i : i + CHUNK]
            ph = ",".join("?" * len(chunk))
            rows = self._conn.execute(
                f"SELECT job_id FROM seen_jobs WHERE job_id IN ({ph}) "
                "AND (ttl IS NULL OR ttl >= ?)",
                (*chunk, now),
            ).fetchall()
            seen.update(r["job_id"] for r in rows)
        return [j for j in candidates if j not in seen]

    def mark_seen(self, job_id: str, *, notified: bool) -> None:
        item = {
            "job_id": job_id,
            "first_seen": datetime.now(timezone.utc).isoformat(),
            "notified": notified,
            "ttl": _ttl(),
        }
        self._write(item, ignore=False)

    def claim_for_notify(
        self, job_id: str, *, score: int | None = None, rationale: str | None = None,
        gaps: list[str] | None = None, posting: NormalizedPosting | None = None,
    ) -> bool:
        item: dict = {
            "job_id": job_id,
            "first_seen": datetime.now(timezone.utc).isoformat(),
            "notified": True,
            "ttl": _ttl(),
        }
        if score is not None:
            item["score"] = score
        if rationale is not None:
            item["rationale"] = rationale
        if gaps:
            item["gaps"] = gaps
        if posting is not None:
            item.update(posting_display_fields(posting))
        return self._write(item, ignore=True) == 1

    def release_claim(self, job_id: str) -> None:
        self._conn.execute("DELETE FROM seen_jobs WHERE job_id = ?", (job_id,))

    def mark_suppressed(
        self, job_id: str, *, score: int | None,
        rationale: str | None = None, posting: NormalizedPosting | None = None,
    ) -> None:
        item: dict = {
            "job_id": job_id,
            "first_seen": datetime.now(timezone.utc).isoformat(),
            "notified": False,
            "ttl": _ttl(),
        }
        if score is not None:
            item["score"] = score
        if rationale is not None:
            item["rationale"] = rationale
        if posting is not None:
            item.update(posting_display_fields(posting))
        self._write(item, ignore=True)

    def _live_rows(self, where: str, params: tuple = ()) -> list[dict]:
        now = _now_ts()
        rows = self._conn.execute(
            f"SELECT data FROM seen_jobs WHERE (ttl IS NULL OR ttl >= ?) AND {where}",
            (now, *params),
        ).fetchall()
        return [json.loads(r["data"]) for r in rows]

    def list_matches(self) -> list[dict]:
        items = self._live_rows("notified = 1 AND title IS NOT NULL")
        return [match_view(it) for it in items]

    def list_suppressed(self) -> list[dict]:
        items = self._live_rows("notified = 0 AND score IS NOT NULL")
        return [
            {"score": int(it["score"]) if it.get("score") is not None else None,
             "first_seen": it.get("first_seen", "")}
            for it in items
        ]

    def list_suppressed_details(self, *, since_iso: str) -> list[dict]:
        """Full item dicts for score-low suppressed rows (audit view). Rows
        written before mark_suppressed stored display fields come back sparse —
        the audit UI renders those as '(no details recorded)'."""
        return self._live_rows(
            "notified = 0 AND score IS NOT NULL AND first_seen >= ?", (since_iso,)
        )

    def get_suppressed(self, job_id: str) -> dict | None:
        rows = self._live_rows("job_id = ? AND notified = 0", (job_id,))
        return rows[0] if rows else None

    def get_match(self, job_id: str) -> dict | None:
        rows = self._live_rows("job_id = ?", (job_id,))
        if not rows:
            return None
        item = rows[0]
        if "title" not in item or not item.get("notified"):
            return None
        return match_view(item)

    def set_status(self, job_id: str, status: str) -> bool:
        if status not in VALID_STATUSES:
            raise ValueError(f"invalid status: {status!r}")
        with self._write_lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT data FROM seen_jobs WHERE job_id = ? AND (ttl IS NULL OR ttl >= ?)",
                    (job_id, _now_ts()),
                ).fetchone()
                if row is None:
                    self._conn.execute("COMMIT")
                    return False
                item = json.loads(row["data"])
                item["status"] = status
                item.setdefault("history", []).append(
                    {"status": status, "at": datetime.now(timezone.utc).isoformat()}
                )
                ttl = item.get("ttl")
                if status in _KEPT_STATUSES:
                    item.pop("ttl", None)
                    ttl = None
                self._conn.execute(
                    "UPDATE seen_jobs SET ttl = ?, data = ? WHERE job_id = ?",
                    (ttl, json.dumps(item), job_id),
                )
                self._conn.execute("COMMIT")
                return True
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def set_audit_verdict(self, job_id: str, verdict: str) -> bool:
        """Stamp an audit judgment on a suppressed row's data JSON. Also
        clears the ttl COLUMN (not just the JSON field) so a verdicted row
        survives past its 60-day TTL: _live_rows/prune_expired both filter on
        the column, so leaving it set would let a confirmed row silently
        expire before the tuning CLI's ~10-verdict sample can accumulate.
        Returns False when the row is absent/expired."""
        if verdict not in ("rescued", "confirmed_rejected"):
            raise ValueError(f"invalid audit verdict: {verdict!r}")
        with self._write_lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT data FROM seen_jobs WHERE job_id = ? AND (ttl IS NULL OR ttl >= ?)",
                    (job_id, _now_ts()),
                ).fetchone()
                if row is None:
                    self._conn.execute("COMMIT")
                    return False
                item = json.loads(row["data"])
                item.pop("ttl", None)
                item["audit_verdict"] = verdict
                item["audit_verdict_at"] = datetime.now(timezone.utc).isoformat()
                self._conn.execute(
                    "UPDATE seen_jobs SET data = ?, ttl = NULL WHERE job_id = ?",
                    (json.dumps(item), job_id),
                )
                self._conn.execute("COMMIT")
                return True
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def mark_rescued_from_suppression(self, job_id: str, *, suppressed_score: int) -> bool:
        """Stamp a rescue verdict + the ORIGINAL suppressed score onto the
        now-notified row a score_low /audit rescue produced (release_claim +
        claim_for_notify already ran, so `job_id` now points at a fresh
        notified row). Without this, analyze_score_low can never see a
        rescued sample — the suppressed row (and its score) is gone by the
        time the rescue completes.

        Also clears the ttl COLUMN (not just the JSON field) so the rescued
        label survives past the 60-day TTL: _live_rows filters on the ttl
        column, so leaving it set would let this row silently expire out from
        under the tuning CLI. Returns False when the row is absent/expired."""
        with self._write_lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT data FROM seen_jobs WHERE job_id = ? AND (ttl IS NULL OR ttl >= ?)",
                    (job_id, _now_ts()),
                ).fetchone()
                if row is None:
                    self._conn.execute("COMMIT")
                    return False
                item = json.loads(row["data"])
                item.pop("ttl", None)
                item["audit_verdict"] = "rescued"
                item["suppressed_score"] = suppressed_score
                item["audit_verdict_at"] = datetime.now(timezone.utc).isoformat()
                self._conn.execute(
                    "UPDATE seen_jobs SET data = ?, ttl = NULL WHERE job_id = ?",
                    (json.dumps(item), job_id),
                )
                self._conn.execute("COMMIT")
                return True
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def list_rescued_suppressions(self, *, since_iso: str) -> list[dict]:
        """Live notified rows carrying a rescue-from-suppression verdict,
        shaped so analyze_score_low consumes them identically to suppressed
        rows: each dict's `score` is overwritten with the ORIGINAL suppressed
        score (not whatever re-score claim_for_notify wrote), and
        `audit_verdict` reads 'rescued'."""
        items = self._live_rows(
            "notified = 1 AND first_seen >= ? "
            "AND json_extract(data, '$.suppressed_score') IS NOT NULL",
            (since_iso,),
        )
        return [{**it, "score": it["suppressed_score"]} for it in items]

    def update_closed_check(self, job_id: str, *, misses: int, closed_at: str | None) -> bool:
        """Persist the closed-check state machine's fields on a card. closed_at
        None clears the flag AND the notified marker (a reappearing posting
        un-flags and re-arms the announcement). False when the row is gone."""
        with self._write_lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT data FROM seen_jobs WHERE job_id = ? AND (ttl IS NULL OR ttl >= ?)",
                    (job_id, _now_ts()),
                ).fetchone()
                if row is None:
                    self._conn.execute("COMMIT")
                    return False
                item = json.loads(row["data"])
                item["closed_misses"] = misses
                if closed_at is None:
                    item.pop("posting_closed_at", None)
                    item.pop("closed_notified", None)
                else:
                    item["posting_closed_at"] = closed_at
                self._conn.execute(
                    "UPDATE seen_jobs SET data = ? WHERE job_id = ?",
                    (json.dumps(item), job_id),
                )
                self._conn.execute("COMMIT")
                return True
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def mark_closed_notified(self, job_id: str) -> bool:
        """Set the once-only digest marker for an announced closure."""
        with self._write_lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT data FROM seen_jobs WHERE job_id = ? AND (ttl IS NULL OR ttl >= ?)",
                    (job_id, _now_ts()),
                ).fetchone()
                if row is None:
                    self._conn.execute("COMMIT")
                    return False
                item = json.loads(row["data"])
                item["closed_notified"] = True
                self._conn.execute(
                    "UPDATE seen_jobs SET data = ? WHERE job_id = ?",
                    (json.dumps(item), job_id),
                )
                self._conn.execute("COMMIT")
                return True
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def update_email_suggestion(
        self, job_id: str, *, suggestion: dict | None, record_dismissed: bool = False
    ) -> bool:
        """Set or clear the gmail-ingest suggestion on a card. Clearing with
        record_dismissed=True remembers the outgoing suggestion's message_id
        (capped list) so the same email never re-suggests. False when the row
        is gone."""
        with self._write_lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT data FROM seen_jobs WHERE job_id = ? AND (ttl IS NULL OR ttl >= ?)",
                    (job_id, _now_ts()),
                ).fetchone()
                if row is None:
                    self._conn.execute("COMMIT")
                    return False
                item = json.loads(row["data"])
                if suggestion is None:
                    old = item.pop("email_suggestion", None)
                    if record_dismissed and old and old.get("message_id"):
                        dismissed = list(item.get("dismissed_suggestions", []) or [])
                        dismissed.append(old["message_id"])
                        item["dismissed_suggestions"] = dismissed[-20:]
                else:
                    item["email_suggestion"] = suggestion
                self._conn.execute(
                    "UPDATE seen_jobs SET data = ? WHERE job_id = ?",
                    (json.dumps(item), job_id),
                )
                self._conn.execute("COMMIT")
                return True
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def recent_gap_lists(self, since: datetime) -> list[list[str]]:
        items = self._live_rows(
            "notified = 1 AND first_seen >= ?", (since.isoformat(),)
        )
        out: list[list[str]] = []
        for it in items:
            g = it.get("gaps")
            out.append(list(g) if isinstance(g, list) else [])
        return out

    def get_jd(self, job_id: str) -> PostingJD | None:
        rows = self._live_rows("job_id = ?", (job_id,))
        if not rows or not rows[0].get("description_snapshot"):
            return None
        it = rows[0]
        return PostingJD(
            description=sanitize_description(it["description_snapshot"])[0],
            title=it.get("title", ""),
            company=it.get("company", ""),
        )

    def list_score_rows(self, *, since_iso: str = "") -> list[dict]:
        """Scored rows for the tuning CLI's per-source stats and snippet
        pair-mining: job_id, score, notified, title, company, first_seen.
        Company lives only in the data JSON (posting_display_fields)."""
        rows = self._conn.execute(
            "SELECT job_id, score, notified, title, first_seen, data FROM seen_jobs "
            "WHERE score IS NOT NULL AND (ttl IS NULL OR ttl >= ?) "
            "AND (? = '' OR first_seen >= ?) ORDER BY first_seen",
            (_now_ts(), since_iso, since_iso),
        ).fetchall()
        out: list[dict] = []
        for r in rows:
            d = json.loads(r["data"])
            out.append({
                "job_id": r["job_id"],
                "score": r["score"],
                "notified": bool(r["notified"]),
                "title": r["title"] or d.get("title") or "",
                "company": d.get("company") or "",
                "first_seen": r["first_seen"] or "",
            })
        return out

    def prune_expired(self) -> int:
        cur = self._conn.execute(
            "DELETE FROM seen_jobs WHERE ttl IS NOT NULL AND ttl < ?", (_now_ts(),)
        )
        return cur.rowcount


class SqliteSourceStateStore:
    """SQLite twin of state.SourceStateStore. Stores etag/last_modified/payload as JSON."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get(self, connector_name: str) -> ConnectorState:
        return self.get_many([connector_name])[connector_name]

    def get_many(self, connector_names: Iterable[str]) -> dict[str, ConnectorState]:
        names = list(dict.fromkeys(connector_names))
        out: dict[str, ConnectorState] = {n: ConnectorState() for n in names}
        if not names:
            return out
        ph = ",".join("?" * len(names))
        rows = self._conn.execute(
            f"SELECT connector_name, data FROM source_state WHERE connector_name IN ({ph})",
            tuple(names),
        ).fetchall()
        for r in rows:
            d = json.loads(r["data"])
            out[r["connector_name"]] = ConnectorState(
                etag=d.get("etag"), last_modified=d.get("last_modified"),
                payload=d.get("payload"),
            )
        return out

    def put(self, connector_name: str, state: ConnectorState) -> None:
        d: dict = {"last_fetched_at": datetime.now(timezone.utc).isoformat()}
        if state.etag is not None:
            d["etag"] = state.etag
        if state.last_modified is not None:
            d["last_modified"] = state.last_modified
        if state.payload is not None:
            d["payload"] = state.payload
        self._conn.execute(
            "INSERT OR REPLACE INTO source_state (connector_name, data) VALUES (?, ?)",
            (connector_name, json.dumps(d)),
        )


class SqliteDiscoveredSlugsStore:
    """SQLite twin of state.DiscoveredSlugsStore. Stores the full item dict as
    JSON and reuses state._row_from_item for identical row shaping."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def _put(self, item: dict) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO discovered_slugs (connector_name, data) VALUES (?, ?)",
            (item["connector_name"], json.dumps({k: v for k, v in item.items() if v is not None})),
        )

    def get(self, connector_name: str) -> DiscoveredSlug | None:
        row = self._conn.execute(
            "SELECT data FROM discovered_slugs WHERE connector_name = ?", (connector_name,)
        ).fetchone()
        return _row_from_item(json.loads(row["data"])) if row else None

    def list_all(self) -> list[DiscoveredSlug]:
        rows = self._conn.execute("SELECT data FROM discovered_slugs").fetchall()
        return [_row_from_item(json.loads(r["data"])) for r in rows]

    def list_healthy(self) -> list[DiscoveredSlug]:
        return [r for r in self.list_all() if r.validation_status == "ok"]

    def upsert_ok(self, connector_name: str, *, company_name: str | None = None,
                  last_posting_count: int = 0) -> None:
        now = datetime.now(timezone.utc).isoformat()
        ats_family, slug = connector_name.split(":", 1)
        existing = self.get(connector_name)
        self._put({
            "connector_name": connector_name, "ats_family": ats_family, "slug": slug,
            "company_name": company_name or (existing.company_name if existing else None),
            "discovered_at": existing.discovered_at if existing else now,
            "last_validated_at": now, "validation_status": "ok",
            "consecutive_failures": 0, "last_posting_count": last_posting_count,
        })

    def upsert_failed(self, connector_name: str, *, quarantine_threshold: int = 5) -> None:
        now = datetime.now(timezone.utc).isoformat()
        ats_family, slug = connector_name.split(":", 1)
        existing = self.get(connector_name)
        failures = (existing.consecutive_failures if existing else 0) + 1
        status = "quarantined" if failures >= quarantine_threshold else "failed"
        self._put({
            "connector_name": connector_name, "ats_family": ats_family, "slug": slug,
            "company_name": existing.company_name if existing else None,
            "discovered_at": existing.discovered_at if existing else now,
            "last_validated_at": now, "validation_status": status,
            "consecutive_failures": failures,
            "last_posting_count": existing.last_posting_count if existing else 0,
        })

    def upsert_no_match(
        self,
        slug: str,
        *,
        company_name: str | None = None,
        website: str | None = None,
        methods_tried: list[str] | None = None,
    ) -> None:
        """Twin of state.DiscoveredSlugsStore.upsert_no_match — omitted kwargs
        preserve the existing row's learned fields."""
        now = datetime.now(timezone.utc).isoformat()
        existing = self.get(f"nomatch:{slug}")
        self._put({
            "connector_name": f"nomatch:{slug}", "ats_family": "nomatch", "slug": slug,
            "company_name": company_name or (existing.company_name if existing else None),
            "discovered_at": existing.discovered_at if existing else now,
            "last_validated_at": now, "validation_status": "no_match",
            "consecutive_failures": 0, "last_posting_count": 0,
            "website": website or (existing.website if existing else None),
            "methods_tried": methods_tried if methods_tried is not None
                             else (existing.methods_tried if existing else None),
        })

    def upsert_candidate(self, connector_name: str, *, company_name: str | None = None,
                         origin: str | None = None, claimed_family: str | None = None,
                         website: str | None = None) -> None:
        """Stage a sighted-but-unprobed candidate. No-op if ANY row already
        exists under this key — never demote a validated/failed/no_match row."""
        if self.get(connector_name) is not None:
            return
        now = datetime.now(timezone.utc).isoformat()
        ats_family, slug = connector_name.split(":", 1)
        self._put({
            "connector_name": connector_name, "ats_family": ats_family, "slug": slug,
            "company_name": company_name, "discovered_at": now, "sighted_at": now,
            "validation_status": "candidate", "consecutive_failures": 0,
            "last_posting_count": 0, "origin": origin, "claimed_family": claimed_family,
            "website": website,
        })

    def list_candidates(self) -> list[DiscoveredSlug]:
        return [r for r in self.list_all() if r.validation_status == "candidate"]

    def list_no_match(self) -> list[DiscoveredSlug]:
        return [r for r in self.list_all() if r.validation_status == "no_match"]

    def delete(self, connector_name: str) -> None:
        self._conn.execute(
            "DELETE FROM discovered_slugs WHERE connector_name = ?", (connector_name,)
        )

    def resolve_candidate_no_match(self, connector_name: str) -> None:
        """A drained candidate that fully missed: overwrite IN PLACE to no_match
        so it leaves the drain queue while capture-dedup still sees the key."""
        row = self.get(connector_name)
        if row is None or row.validation_status != "candidate":
            return
        now = datetime.now(timezone.utc).isoformat()
        self._put({
            "connector_name": row.connector_name, "ats_family": row.ats_family,
            "slug": row.slug, "company_name": row.company_name,
            "discovered_at": row.discovered_at, "last_validated_at": now,
            "validation_status": "no_match", "consecutive_failures": 0,
            "last_posting_count": 0, "origin": row.origin,
            "sighted_at": row.sighted_at, "claimed_family": row.claimed_family,
        })

    def is_recent_no_match(self, slug: str, *, fresh_within_days: int) -> bool:
        row = self.get(f"nomatch:{slug}")
        if row is None or not row.last_validated_at:
            return False
        try:
            dt = datetime.fromisoformat(row.last_validated_at)
        except ValueError:
            return False
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt) < timedelta(days=fresh_within_days)

    def list_for_revalidation(self, *, stale_after_days: int, limit: int) -> list[DiscoveredSlug]:
        threshold = datetime.now(timezone.utc) - timedelta(days=stale_after_days)
        out: list[DiscoveredSlug] = []
        for row in self.list_all():
            if row.validation_status != "ok" or not row.last_validated_at:
                continue
            try:
                dt = datetime.fromisoformat(row.last_validated_at)
            except ValueError:
                continue
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if dt < threshold:
                out.append(row)
                if len(out) >= limit:
                    break
        return out


class SqliteDiscoveredBoardsStore:
    """SQLite store for enterprise boards found by run_board_discovery. Keyed by
    seed domain; ok rows carry the resolved FingerprintResult identity so
    build_connectors can reconstruct the connector."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def _put(self, item: dict) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO discovered_boards (domain, data) VALUES (?, ?)",
            (item["domain"], json.dumps({k: v for k, v in item.items() if v is not None})),
        )

    def get(self, domain: str) -> DiscoveredBoard | None:
        row = self._conn.execute(
            "SELECT data FROM discovered_boards WHERE domain = ?", (domain,)
        ).fetchone()
        return _board_from_item(json.loads(row["data"])) if row else None

    def list_all(self) -> list[DiscoveredBoard]:
        rows = self._conn.execute("SELECT data FROM discovered_boards").fetchall()
        return [_board_from_item(json.loads(r["data"])) for r in rows]

    def list_healthy(self) -> list[DiscoveredBoard]:
        return [b for b in self.list_all() if b.status == "ok"]

    def stale_before(self, iso: str) -> list[DiscoveredBoard]:
        # ISO-8601 timestamps sort lexically = chronologically.
        return [b for b in self.list_all() if b.last_swept_at < iso]

    def upsert_ok(self, domain: str, *, name: str, family: str, identity: dict,
                  connector_name: str, company: str | None = None) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self._put({
            "domain": domain, "name": name, "status": "ok", "family": family,
            "identity": identity, "connector_name": connector_name, "company": company,
            "last_swept_at": now, "failure_streak": 0,
        })

    def upsert_result(self, domain: str, *, name: str, status: str,
                      quarantine_threshold: int = 5) -> None:
        """Persist a non-matched sweep result. Only real errors streak toward
        quarantine — benign not_found/unsupported seeds reset the streak so a
        legitimately quiet or unsupported company doesn't get poisoned into
        quarantine after enough re-sweeps."""
        now = datetime.now(timezone.utc).isoformat()
        existing = self.get(domain)
        if status == "error":
            streak = (existing.failure_streak if existing else 0) + 1
            final = "quarantined" if streak >= quarantine_threshold else "error"
        else:
            streak = 0
            final = status
        self._put({
            "domain": domain, "name": name, "status": final,
            "last_swept_at": now, "failure_streak": streak,
        })

    def upsert_candidate(self, domain: str, *, name: str, family: str, identity: dict,
                         connector_name: str | None, company: str | None = None,
                         origin: str | None = None) -> None:
        """Stage a sighted board candidate. No-op if ANY row exists under the
        domain — never demote a swept/ok row. last_swept_at is stamped so
        stale_before's string comparison stays safe."""
        if self.get(domain) is not None:
            return
        now = datetime.now(timezone.utc).isoformat()
        self._put({
            "domain": domain, "name": name, "status": "candidate", "family": family,
            "identity": identity, "connector_name": connector_name, "company": company,
            "last_swept_at": now, "failure_streak": 0,
            "origin": origin, "sighted_at": now,
        })

    def list_candidates(self) -> list[DiscoveredBoard]:
        return [b for b in self.list_all() if b.status == "candidate"]

    def upsert_candidate_failed(self, domain: str, *, quarantine_threshold: int = 5) -> None:
        """A candidate that failed live-verify: streak++, stays 'candidate'
        (retried next discovery run) until the threshold, then quarantined."""
        existing = self.get(domain)
        if existing is None or existing.status != "candidate":
            return
        now = datetime.now(timezone.utc).isoformat()
        streak = existing.failure_streak + 1
        status = "quarantined" if streak >= quarantine_threshold else "candidate"
        self._put({
            "domain": existing.domain, "name": existing.name, "status": status,
            "family": existing.family, "identity": existing.identity,
            "connector_name": existing.connector_name, "company": existing.company,
            "last_swept_at": now, "failure_streak": streak,
            "origin": existing.origin, "sighted_at": existing.sighted_at,
        })


class SqliteConnectorHealthStore:
    """SQLite twin of state.ConnectorHealthStore. A row exists only while a
    connector is unhealthy (dead_streak > 0 or suppressed)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def suppressed_names(self) -> set[str]:
        rows = self._conn.execute(
            "SELECT connector_name FROM connector_health WHERE suppressed = 1"
        ).fetchall()
        return {r["connector_name"] for r in rows}

    def tracked_names(self) -> set[str]:
        rows = self._conn.execute("SELECT connector_name FROM connector_health").fetchall()
        return {r["connector_name"] for r in rows}

    def record_dead(self, connector_name: str) -> int:
        now = datetime.now(timezone.utc).isoformat()
        row = self._conn.execute(
            "INSERT INTO connector_health (connector_name, dead_streak, suppressed, updated_at) "
            "VALUES (?, 1, 0, ?) "
            "ON CONFLICT(connector_name) DO UPDATE SET "
            "dead_streak = dead_streak + 1, updated_at = excluded.updated_at "
            "RETURNING dead_streak",
            (connector_name, now),
        ).fetchone()
        return int(row["dead_streak"])

    def mark_suppressed(self, connector_name: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            "INSERT INTO connector_health (connector_name, dead_streak, suppressed, updated_at) "
            "VALUES (?, 0, 1, ?) "
            "ON CONFLICT(connector_name) DO UPDATE SET suppressed = 1, updated_at = excluded.updated_at",
            (connector_name, now),
        )

    def clear(self, connector_name: str) -> None:
        self._conn.execute(
            "DELETE FROM connector_health WHERE connector_name = ?", (connector_name,)
        )

    def mark_backoff(self, connector_name: str, until_ms: int) -> None:
        """Temporarily skip a connector until `until_ms` (epoch ms) — a soft,
        self-expiring pause for rate-limited (429) hosts. Distinct from the
        permanent dead-suppression path; does not touch dead_streak/suppressed."""
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            "INSERT INTO connector_health (connector_name, dead_streak, suppressed, updated_at, backoff_until) "
            "VALUES (?, 0, 0, ?, ?) "
            "ON CONFLICT(connector_name) DO UPDATE SET "
            "backoff_until = excluded.backoff_until, updated_at = excluded.updated_at",
            (connector_name, now, int(until_ms)),
        )

    def backoff_names(self, now_ms: int) -> set[str]:
        """Connectors whose backoff window hasn't expired yet (skip this cycle)."""
        rows = self._conn.execute(
            "SELECT connector_name FROM connector_health WHERE backoff_until > ?",
            (int(now_ms),),
        ).fetchall()
        return {r["connector_name"] for r in rows}


class SqlitePipelineEventsStore:
    """Local-runtime cycle telemetry. The poller appends one row per completed
    ats/slow cycle; the /pipeline ops page aggregates the recent window into
    that view.

    Rows older than _RETENTION_DAYS are pruned on each write so the table stays
    bounded under the every-few-minutes ats cadence."""

    _RETENTION_DAYS = 14

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._write_lock = threading.Lock()

    def record_cycle(
        self,
        *,
        tier: str,
        fetched: int,
        matched: int,
        notified: int,
        duration_ms: int,
        failures: list[dict] | None = None,
        llm_failures: list[dict] | None = None,
        new_count: int | None = None,
    ) -> None:
        """Append one cycle's outcome. `failures` is the orchestrator's
        fetch_failures list ({"source","error_type"}); a non-empty list flags the
        cycle as not-ok. `llm_failures` is the relevance/gap fallback list
        ({"stage","error_type"}); it is recorded for the /pipeline degraded
        indicator but does NOT affect `ok` (fetch health is separate)."""
        failures = failures or []
        llm_failures = llm_failures or []
        now = _now_ms()
        with self._write_lock:
            self._conn.execute(
                "INSERT INTO pipeline_events "
                "(ts_ms, tier, fetched, matched, notified, duration_ms, ok, failures, llm_failures, new_count) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (now, tier, int(fetched), int(matched), int(notified),
                 int(duration_ms), 0 if failures else 1,
                 json.dumps(failures), json.dumps(llm_failures),
                 int(new_count) if new_count is not None else None),
            )
            # Prune old rows but always keep each tier's newest row — it is the
            # unwindowed anchor that keeps a dead tier's stall warning alive on
            # /pipeline after its rows age past the window and retention.
            self._conn.execute(
                "DELETE FROM pipeline_events WHERE ts_ms < ? AND ts_ms NOT IN "
                "(SELECT MAX(ts_ms) FROM pipeline_events GROUP BY tier)",
                (now - self._RETENTION_DAYS * 86_400_000,),
            )

    def recent_cycles(self, window_days: int, *, now_ms: int | None = None) -> list[dict]:
        """Cycle rows within the trailing window, oldest first, for aggregation."""
        now = now_ms if now_ms is not None else _now_ms()
        cutoff = now - window_days * 86_400_000
        rows = self._conn.execute(
            "SELECT ts_ms, tier, fetched, matched, notified, duration_ms, ok, failures, llm_failures, new_count "
            "FROM pipeline_events WHERE ts_ms >= ? ORDER BY ts_ms",
            (cutoff,),
        ).fetchall()
        return [
            {
                "ts_ms": r["ts_ms"], "tier": r["tier"], "fetched": r["fetched"],
                "matched": r["matched"], "notified": r["notified"],
                "duration_ms": r["duration_ms"], "ok": bool(r["ok"]),
                "failures": json.loads(r["failures"]),
                "llm_failures": json.loads(r["llm_failures"]),
                "new_count": r["new_count"],
            }
            for r in rows
        ]

    def last_success_ms(self) -> int | None:
        """Epoch-ms of the most recent fully-successful (ok) cycle, or None."""
        row = self._conn.execute(
            "SELECT MAX(ts_ms) AS ts FROM pipeline_events WHERE ok = 1"
        ).fetchone()
        return int(row["ts"]) if row and row["ts"] is not None else None

    def last_cycle_ms(self) -> int | None:
        """Epoch-ms of the most recent cycle row regardless of ok — liveness
        signal for the pipeline-stopped watchdog (a failing poller is still a
        running poller)."""
        row = self._conn.execute("SELECT MAX(ts_ms) AS ts FROM pipeline_events").fetchone()
        return int(row["ts"]) if row and row["ts"] is not None else None

    def last_cycle_per_tier(self) -> list[dict]:
        """Each tier's newest row regardless of window — the anchor that keeps
        a long-dead tier visible as stalled on /pipeline. SQLite's
        bare-columns-with-MAX semantics guarantee ok/llm_failures come from
        the newest row."""
        rows = self._conn.execute(
            "SELECT tier, MAX(ts_ms) AS ts_ms, ok, llm_failures "
            "FROM pipeline_events GROUP BY tier"
        ).fetchall()
        return [
            {
                "tier": r["tier"], "ts_ms": int(r["ts_ms"]), "ok": bool(r["ok"]),
                "degraded": bool(json.loads(r["llm_failures"])),
            }
            for r in rows
        ]


class SqliteOpsAlertStateStore:
    """Per-condition ops-alert state (active flag + last-sent timestamp) so
    cooldowns and recovery notices survive process restarts. Shared by the
    poller (llm_degraded/zero_yield) and the web watchdog (pipeline_stopped)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get(self, condition: str) -> dict:
        row = self._conn.execute(
            "SELECT active, last_sent_ms FROM ops_alert_state WHERE condition = ?",
            (condition,),
        ).fetchone()
        if row is None:
            return {"active": False, "last_sent_ms": None}
        return {"active": bool(row["active"]), "last_sent_ms": row["last_sent_ms"]}

    def mark_active(self, condition: str, *, now_ms: int) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO ops_alert_state (condition, active, last_sent_ms) "
            "VALUES (?, 1, ?)",
            (condition, now_ms),
        )

    def mark_recovered(self, condition: str) -> None:
        self._conn.execute(
            "UPDATE ops_alert_state SET active = 0 WHERE condition = ?", (condition,)
        )


class SqliteRejectedPostingsStore:
    """Audit trail of filter-gate rejections. Capture-once: INSERT OR IGNORE
    on job_id, so a posting re-fetched and re-rejected on later cycles keeps
    its first record. Retention is enforced by the scheduler's daily prune,
    not on write."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def source_family_windows(
        self, *, baseline_days: int, silence_hours: float, now: datetime | None = None,
    ) -> dict[str, tuple[int, int, int]]:
        """Per-connector-family delivery volume, split into a baseline window and
        a recent one: ``{family: (baseline_rows, baseline_active_days, recent_rows)}``.

        Family, not individual board: a single greenhouse slug going quiet for a
        week is routine, while the whole greenhouse family going quiet is not.
        Rejections are the right denominator — they are the bulk of what a source
        delivers, so they measure "is anything arriving at all" rather than "is
        anything good arriving", which legitimately varies.

        Reads only this table. Matched postings live in seen_jobs but are a rounding
        error against rejections (1.8k vs 220k), and a source delivering solely
        matches for a whole window is not a real shape."""
        base_start = (now or datetime.now(timezone.utc)) - timedelta(days=baseline_days)
        recent_start = (now or datetime.now(timezone.utc)) - timedelta(hours=silence_hours)
        out: dict[str, list[int | set]] = {}
        rows = self._conn.execute(
            "SELECT source, date(first_seen) AS d, "
            "       SUM(CASE WHEN first_seen >= ? THEN 1 ELSE 0 END) AS recent, "
            "       COUNT(*) AS total "
            "FROM rejected_postings WHERE first_seen >= ? AND source IS NOT NULL "
            "GROUP BY source, d",
            (recent_start.isoformat(), base_start.isoformat()),
        ).fetchall()
        for source, day, recent, total in rows:
            family = (source or "").split(":", 1)[0]
            if not family:
                continue
            slot = out.setdefault(family, [0, set(), 0])
            baseline_rows = total - recent
            slot[0] += baseline_rows
            if baseline_rows:
                slot[1].add(day)   # type: ignore[union-attr]
            slot[2] += recent
        return {f: (v[0], len(v[1]), v[2]) for f, v in out.items()}  # type: ignore[arg-type]

    def record(self, posting: NormalizedPosting, *, rejected_by: str) -> bool:
        now = datetime.now(timezone.utc).isoformat()
        item: dict = {
            "job_id": posting.job_id,
            "first_seen": now,
            "rejected_by": rejected_by,
            "location_tags": sorted(posting.location_tags),
            "stack": sorted(posting.stack),
            "seniority": posting.seniority,
            **posting_display_fields(posting),
        }
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO rejected_postings "
            "(job_id, first_seen, rejected_by, title, company, location_text, "
            " source, apply_url, posted_at, comp_min, comp_max, data) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (posting.job_id, now, rejected_by, posting.title, posting.company,
             posting.location_text, posting.source, posting.apply_url,
             posting.posted_at.isoformat() if posting.posted_at else None,
             posting.comp_min, posting.comp_max, json.dumps(item)),
        )
        return cur.rowcount == 1

    def list_rejected(
        self, *, since_iso: str, gate: str | None = None, query: str = "",
        include_judged: bool = True, limit: int = 500,
    ) -> list[dict]:
        sql = ("SELECT data, rejected_by, verdict, verdict_at FROM rejected_postings "
               "WHERE first_seen >= ?")
        params: list = [since_iso]
        if gate:
            sql += " AND rejected_by = ?"
            params.append(gate)
        if query:
            like = f"%{query}%"
            sql += " AND (title LIKE ? OR company LIKE ? OR source LIKE ?)"
            params.extend([like, like, like])
        if not include_judged:
            sql += " AND verdict IS NULL"
        sql += " ORDER BY first_seen DESC LIMIT ?"
        params.append(limit)
        out: list[dict] = []
        for r in self._conn.execute(sql, params):
            item = json.loads(r["data"])
            item["rejected_by"] = r["rejected_by"]
            item["verdict"] = r["verdict"]
            item["verdict_at"] = r["verdict_at"]
            out.append(item)
        return out

    def counts_by_gate(self, *, since_iso: str) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT rejected_by, COUNT(*) AS c FROM rejected_postings "
            "WHERE first_seen >= ? GROUP BY rejected_by",
            (since_iso,),
        ).fetchall()
        return {r["rejected_by"]: r["c"] for r in rows}

    def get(self, job_id: str) -> dict | None:
        r = self._conn.execute(
            "SELECT data, rejected_by, verdict, verdict_at FROM rejected_postings "
            "WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        if r is None:
            return None
        item = json.loads(r["data"])
        item["rejected_by"] = r["rejected_by"]
        item["verdict"] = r["verdict"]
        item["verdict_at"] = r["verdict_at"]
        return item

    def set_verdict(self, job_id: str, verdict: str) -> bool:
        if verdict not in ("rescued", "confirmed_rejected"):
            raise ValueError(f"invalid audit verdict: {verdict!r}")
        cur = self._conn.execute(
            "UPDATE rejected_postings SET verdict = ?, verdict_at = ? WHERE job_id = ?",
            (verdict, datetime.now(timezone.utc).isoformat(), job_id),
        )
        return cur.rowcount == 1

    def prune_older_than(self, days: int) -> int:
        """Delete rejected rows older than the cutoff, except ones a human
        has already stamped with an audit verdict — those are retained
        indefinitely so the tuning CLI's ~10-verdict sample can accumulate."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        cur = self._conn.execute(
            "DELETE FROM rejected_postings WHERE first_seen < ? AND verdict IS NULL",
            (cutoff,),
        )
        return cur.rowcount


class SqliteCoachRunsStore:
    """Persisted /coach runs. Each row is one LLM run: the snapshot sent, the
    cards that came back, and an ok/error status. Runs are small and manual,
    so nothing schedules prune_older_than yet — it exists for a future
    retention knob."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def record(
        self, *, run_id: str, provider: str = "", model: str = "", status: str,
        snapshot_json: str = "", result_json: str = "", error: str | None = None,
        created_at: str | None = None,
    ) -> None:
        created = created_at or datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            "INSERT OR REPLACE INTO coach_runs "
            "(run_id, created_at, provider, model, status, snapshot, result, error) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (run_id, created, provider, model, status, snapshot_json, result_json, error),
        )

    @staticmethod
    def _to_run(r: sqlite3.Row) -> dict:
        return {
            "run_id": r["run_id"], "created_at": r["created_at"],
            "provider": r["provider"], "model": r["model"],
            "status": r["status"], "error": r["error"],
            "cards": json.loads(r["result"]) if r["result"] else [],
            "snapshot": json.loads(r["snapshot"]) if r["snapshot"] else None,
        }

    def get(self, run_id: str) -> dict | None:
        r = self._conn.execute(
            "SELECT * FROM coach_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        return self._to_run(r) if r is not None else None

    def latest(self) -> dict | None:
        r = self._conn.execute(
            "SELECT * FROM coach_runs ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        return self._to_run(r) if r is not None else None

    def list_runs(self, *, limit: int = 20) -> list[dict]:
        rows = self._conn.execute(
            "SELECT run_id, created_at, provider, model, status, error "
            "FROM coach_runs ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def prune_older_than(self, days: int) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        cur = self._conn.execute(
            "DELETE FROM coach_runs WHERE created_at < ?", (cutoff,)
        )
        return cur.rowcount


class SqliteBuilderSettingsStore:
    """The résumé-builder settings singleton. One JSON row — validation lives
    in src.tailor.render.settings, not here."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get(self) -> dict:
        r = self._conn.execute("SELECT data FROM builder_settings WHERE id = 1").fetchone()
        return json.loads(r["data"]) if r is not None else {}

    def put(self, data: dict) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO builder_settings (id, data) VALUES (1, ?)",
            (json.dumps(data),),
        )
