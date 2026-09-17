from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable

import boto3
from botocore.exceptions import ClientError

from src.models import ConnectorState, NormalizedPosting
from src.normalize import workplace_type_from_tags
from src.sanitize import sanitize_description

_BATCH_GET_LIMIT = 100
_TTL_DAYS = 60
# Public: the triage UI's web route imports this as the shared source of truth
# for valid status values. _KEPT_STATUSES stays private to set_status.
VALID_STATUSES = frozenset(
    {"new", "interested", "applied", "interviewing", "offer", "rejected", "ghosted", "dismissed"}
)
_KEPT_STATUSES = frozenset(
    {"interested", "applied", "interviewing", "offer", "rejected", "ghosted"}
)


def posting_display_fields(posting: NormalizedPosting) -> dict:
    """The display + JD fields persisted onto a seen_jobs / rejected_postings
    item for a posting: what the triage UI renders plus description_snapshot
    (capped 30 KB) for the tailor endpoint and audit rescue re-scoring."""
    item: dict = {
        "title": posting.title,
        "company": posting.company,
        "location_text": posting.location_text,
        "apply_url": posting.apply_url,
        "source": posting.source,
    }
    if posting.description:
        item["description_snapshot"] = posting.description[:30000]
    if posting.employment_type is not None:
        item["employment_type"] = posting.employment_type
    # Persist the authoritative workplace bucket derived from location_tags (which
    # already folds in the remote flag + ATS enum) so triage can filter on it
    # without re-guessing from free text. Omitted when tags carry no workplace
    # signal — the triage row then falls back to workplace_type_of(location_text).
    workplace_type = workplace_type_from_tags(posting.location_tags)
    if workplace_type is not None:
        item["workplace_type"] = workplace_type
    if posting.comp_min is not None:
        item["comp_min"] = posting.comp_min
    if posting.comp_max is not None:
        item["comp_max"] = posting.comp_max
    if posting.posted_at is not None:
        item["posted_at"] = posting.posted_at.isoformat()
    return item


def posting_from_item(item: dict) -> NormalizedPosting:
    """Reconstruct a NormalizedPosting from a stored item dict (inverse of
    posting_display_fields plus the normalized extras the rejected store
    records). Missing fields default to empty so sparse pre-migration rows
    still reconstruct — callers gate on what they need (e.g. title)."""
    posted_at = None
    if item.get("posted_at"):
        try:
            posted_at = datetime.fromisoformat(item["posted_at"])
        except ValueError:
            posted_at = None
    def _int(v):
        return int(v) if v is not None else None
    return NormalizedPosting(
        job_id=item["job_id"],
        title=item.get("title", ""),
        company=item.get("company", ""),
        location_text=item.get("location_text", ""),
        location_tags=frozenset(item.get("location_tags", []) or []),
        seniority=item.get("seniority"),
        stack=frozenset(item.get("stack", []) or []),
        comp_min=_int(item.get("comp_min")),
        comp_max=_int(item.get("comp_max")),
        apply_url=item.get("apply_url", ""),
        # Sanitize on read, not just on write: rows stored before the filter
        # existed still hold raw text, and the audit page feeds this straight
        # to the scorer. Doing it here avoids a migration over ~220k rows and
        # keeps the guarantee tied to the read rather than to when the row
        # happened to be written.
        description=sanitize_description(item.get("description_snapshot", "") or "")[0],
        posted_at=posted_at,
        source=item.get("source", ""),
        employment_type=item.get("employment_type"),
    )


def match_view(item: dict) -> dict:
    """Normalize a raw stored seen_jobs item into the triage view shape: coerce
    numbers to int, default absent status to 'new', gaps to []."""
    def _int(v):
        return int(v) if v is not None else None
    return {
        "job_id": item["job_id"],
        "title": item.get("title", ""),
        "company": item.get("company", ""),
        "location_text": item.get("location_text", ""),
        "workplace_type": item.get("workplace_type"),
        "comp_min": _int(item.get("comp_min")),
        "comp_max": _int(item.get("comp_max")),
        "apply_url": item.get("apply_url", ""),
        "source": item.get("source", ""),
        "posted_at": item.get("posted_at"),
        "score": _int(item.get("score")),
        "rationale": item.get("rationale"),
        "gaps": list(item.get("gaps", []) or []),
        "first_seen": item.get("first_seen", ""),
        "status": item.get("status", "new"),
        "history": list(item.get("history", []) or []),
        "posting_closed_at": item.get("posting_closed_at"),
        "closed_misses": int(item.get("closed_misses", 0) or 0),
        "closed_notified": bool(item.get("closed_notified", False)),
        "email_suggestion": item.get("email_suggestion"),
        "dismissed_suggestions": list(item.get("dismissed_suggestions", []) or []),
    }


class SeenJobsStore:
    def __init__(self, table_name: str, *, region: str = "us-east-1") -> None:
        self._table_name = table_name
        self._ddb = boto3.resource("dynamodb", region_name=region)
        self._table = self._ddb.Table(table_name)
        self._client = boto3.client("dynamodb", region_name=region)

    def diff_new(self, job_ids: Iterable[str]) -> list[str]:
        """Return the subset of job_ids not present in the table."""
        candidates = list(dict.fromkeys(job_ids))  # dedup, preserve order
        if not candidates:
            return []
        seen: set[str] = set()
        for i in range(0, len(candidates), _BATCH_GET_LIMIT):
            chunk = candidates[i : i + _BATCH_GET_LIMIT]
            keys = [{"job_id": {"S": j}} for j in chunk]
            resp = self._client.batch_get_item(
                RequestItems={self._table_name: {"Keys": keys, "ProjectionExpression": "job_id"}}
            )
            for item in resp["Responses"].get(self._table_name, []):
                seen.add(item["job_id"]["S"])
            unprocessed = resp.get("UnprocessedKeys", {}).get(self._table_name)
            if unprocessed:
                # boto3 typically retries; treat any leftover as not-seen so we don't miss.
                pass
        return [j for j in candidates if j not in seen]

    def mark_seen(self, job_id: str, *, notified: bool) -> None:
        now = datetime.now(timezone.utc)
        ttl = int((now + timedelta(days=_TTL_DAYS)).timestamp())
        self._table.put_item(
            Item={
                "job_id": job_id,
                "first_seen": now.isoformat(),
                "notified": notified,
                "ttl": ttl,
            }
        )

    def claim_for_notify(
        self,
        job_id: str,
        *,
        score: int | None = None,
        rationale: str | None = None,
        gaps: list[str] | None = None,
        posting: NormalizedPosting | None = None,
    ) -> bool:
        """Atomically reserve `job_id` for notification.

        Returns True when this caller wrote the entry (no prior row existed),
        False when another concurrent invocation already claimed it. Used to
        prevent two EventBridge-triggered Lambdas from notifying the same job.

        ``score``/``rationale`` are the LLM relevance verdict for the posting;
        when present they are persisted on the row so a notified job's score can
        be explained after the fact (rows self-expire via the 60-day TTL). They
        are omitted when relevance scoring is disabled or did not run.

        ``gaps`` is the list of résumé skill gaps identified for the posting;
        omitted when ``None`` or empty.

        ``posting`` carries the human-readable display fields (title, company,
        comp, etc.) for the triage UI. The full ``description`` is stored
        (capped at 30 KB) as ``description_snapshot`` so the hosted tailor
        endpoint can tailor against it.
        """
        now = datetime.now(timezone.utc)
        ttl = int((now + timedelta(days=_TTL_DAYS)).timestamp())
        item: dict = {
            "job_id": job_id,
            "first_seen": now.isoformat(),
            "notified": True,
            "ttl": ttl,
        }
        if score is not None:
            item["score"] = score
        if rationale is not None:
            item["rationale"] = rationale
        if gaps:
            item["gaps"] = gaps
        if posting is not None:
            item.update(posting_display_fields(posting))
        try:
            self._table.put_item(
                Item=item,
                ConditionExpression="attribute_not_exists(job_id)",
            )
            return True
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return False
            raise

    def release_claim(self, job_id: str) -> None:
        """Drop a previously-claimed entry so a later invocation can retry.

        Only call this when `claim_for_notify` returned True and the subsequent
        notification fully failed (no sink succeeded). Idempotent if the entry
        is already gone.
        """
        self._table.delete_item(Key={"job_id": job_id})

    def mark_suppressed(
        self, job_id: str, *, score: int | None,
        rationale: str | None = None, posting: NormalizedPosting | None = None,
    ) -> None:
        """Record a posting the LLM scored at/below `score_low` so `diff_new`
        excludes it on later cycles — this stops the every-cycle re-fetch and
        re-score of the same suppressed posting.

        Written with ``notified=False`` so it stays out of the triage inbox
        (``list_matches`` filters ``notified=true``); the ``score`` feeds the
        pipeline score histogram via ``list_suppressed``. Display fields (title,
        company, etc.) ARE stored when ``posting`` is provided, for the audit
        view — omitted otherwise so cycles that skip scoring stay sparse.

        Conditional on ``attribute_not_exists(job_id)``: if a row already exists
        (e.g. a concurrent cycle already notified this job), this is a silent
        no-op so a notified row is never overwritten.
        """
        now = datetime.now(timezone.utc)
        ttl = int((now + timedelta(days=_TTL_DAYS)).timestamp())
        item: dict = {
            "job_id": job_id,
            "first_seen": now.isoformat(),
            "notified": False,
            "ttl": ttl,
        }
        if score is not None:
            item["score"] = score
        if rationale is not None:
            item["rationale"] = rationale
        if posting is not None:
            item.update(posting_display_fields(posting))
        try:
            self._table.put_item(
                Item=item,
                ConditionExpression="attribute_not_exists(job_id)",
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return
            raise

    def list_matches(self) -> list[dict]:
        """All notified rows that carry display fields (title present), shaped
        for the triage UI. Pre-migration rows (no title) are skipped — triage
        data is forward-only."""
        out: list[dict] = []
        params: dict = {
            "FilterExpression": "#n = :true AND attribute_exists(title)",
            "ExpressionAttributeNames": {"#n": "notified"},
            "ExpressionAttributeValues": {":true": True},
        }
        while True:
            resp = self._table.scan(**params)
            for item in resp.get("Items", []):
                out.append(match_view(item))
            last = resp.get("LastEvaluatedKey")
            if not last:
                break
            params["ExclusiveStartKey"] = last
        return out

    def list_suppressed(self) -> list[dict]:
        """All suppressed rows (``notified=false``) that carry a relevance
        ``score``, projected for the pipeline score histogram. These are postings
        the LLM scored at/below ``score_low``; they are deliberately kept out of
        ``list_matches`` (which requires ``notified=true``) so they never enter
        the triage inbox, but they DO feed the score distribution so its low end
        is not always empty. Returns ``[{"score": int, "first_seen": str}]``."""
        out: list[dict] = []
        params: dict = {
            "FilterExpression": "#n = :false AND attribute_exists(#sc)",
            "ExpressionAttributeNames": {"#n": "notified", "#sc": "score", "#fs": "first_seen"},
            "ExpressionAttributeValues": {":false": False},
            "ProjectionExpression": "#sc, #fs",
        }
        while True:
            resp = self._table.scan(**params)
            for item in resp.get("Items", []):
                sc = item.get("score")
                out.append({
                    "score": int(sc) if sc is not None else None,
                    "first_seen": item.get("first_seen", ""),
                })
            last = resp.get("LastEvaluatedKey")
            if not last:
                break
            params["ExclusiveStartKey"] = last
        return out

    def get_match(self, job_id: str) -> dict | None:
        """Point-lookup counterpart to list_matches: returns the triage view for
        one job, or None if the row is missing, not notified, or lacks display
        fields. Gating on `notified` keeps this consistent with list_matches so
        the detail route can't surface a row the list never would."""
        resp = self._table.get_item(Key={"job_id": job_id})
        item = resp.get("Item")
        if not item or "title" not in item or not item.get("notified"):
            return None
        return match_view(item)

    def set_status(self, job_id: str, status: str) -> bool:
        """Set the triage status on an existing match and append a {status, at}
        entry to its history. Moving to a 'kept' status removes the TTL so the row
        persists; new/dismissed leave the TTL in place. Returns False if the row no
        longer exists (e.g. TTL-expired between list and click)."""
        if status not in VALID_STATUSES:
            raise ValueError(f"invalid status: {status!r}")
        entry = {"status": status, "at": datetime.now(timezone.utc).isoformat()}
        names = {"#s": "status", "#h": "history"}
        values = {":s": status, ":entry": [entry], ":empty": []}
        expr = "SET #s = :s, #h = list_append(if_not_exists(#h, :empty), :entry)"
        if status in _KEPT_STATUSES:
            expr += " REMOVE #t"
            names["#t"] = "ttl"
        try:
            self._table.update_item(
                Key={"job_id": job_id},
                UpdateExpression=expr,
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
                ConditionExpression="attribute_exists(job_id)",
            )
            return True
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return False
            raise

    def recent_gap_lists(self, since: datetime) -> list[list[str]]:
        """Return one entry per notified job whose first_seen >= ``since`` — the
        row's stored ``gaps`` list, or ``[]`` for rows without one (clean
        matches). Returning clean matches too gives callers an honest
        denominator (total recent matches, not just those with gaps).

        first_seen is an ISO-8601 UTC string, so a lexicographic >= comparison
        against ``since.isoformat()`` is a correct time comparison."""
        since_iso = since.isoformat()
        out: list[list[str]] = []
        params: dict = {
            "FilterExpression": "#fs >= :since AND #n = :true",
            "ExpressionAttributeNames": {"#fs": "first_seen", "#n": "notified", "#g": "gaps"},
            "ExpressionAttributeValues": {":since": since_iso, ":true": True},
            "ProjectionExpression": "#g",
        }
        while True:
            resp = self._table.scan(**params)
            for item in resp.get("Items", []):
                g = item.get("gaps")
                out.append(list(g) if isinstance(g, list) else [])
            last = resp.get("LastEvaluatedKey")
            if not last:
                break
            params["ExclusiveStartKey"] = last
        return out

    def get_jd(self, job_id: str) -> "PostingJD | None":
        """Read the stored job description for the tailor loop. Returns None when
        the row or its description_snapshot is absent (older alert or TTL-expired).
        Backend-agnostic counterpart used by the local web app."""
        from src.tailor.endpoint.jd import PostingJD
        resp = self._table.get_item(Key={"job_id": job_id})
        item = resp.get("Item")
        if not item or not item.get("description_snapshot"):
            return None
        return PostingJD(
            description=item["description_snapshot"],
            title=item.get("title", ""),
            company=item.get("company", ""),
        )


class SourceStateStore:
    """Persists per-connector ETag / Last-Modified for HTTP conditional GET,
    plus the optional ConnectorState.payload dict."""

    def __init__(self, table_name: str, *, region: str = "us-east-1") -> None:
        self._table_name = table_name
        self._ddb = boto3.resource("dynamodb", region_name=region)
        self._table = self._ddb.Table(table_name)
        self._client = boto3.client("dynamodb", region_name=region)

    def get(self, connector_name: str) -> ConnectorState:
        return self.get_many([connector_name])[connector_name]

    def get_many(self, connector_names: Iterable[str]) -> dict[str, ConnectorState]:
        names = list(dict.fromkeys(connector_names))
        out: dict[str, ConnectorState] = {n: ConnectorState() for n in names}
        if not names:
            return out
        for i in range(0, len(names), _BATCH_GET_LIMIT):
            chunk = names[i : i + _BATCH_GET_LIMIT]
            keys = [{"connector_name": {"S": n}} for n in chunk]
            resp = self._client.batch_get_item(
                RequestItems={
                    self._table_name: {
                        "Keys": keys,
                        "ProjectionExpression": "connector_name, etag, last_modified, payload",
                    }
                }
            )
            for item in resp["Responses"].get(self._table_name, []):
                name = item["connector_name"]["S"]
                etag = item.get("etag", {}).get("S")
                last_modified = item.get("last_modified", {}).get("S")
                payload_raw = item.get("payload", {}).get("S")
                payload = json.loads(payload_raw) if payload_raw else None
                out[name] = ConnectorState(etag=etag, last_modified=last_modified, payload=payload)
        return out

    def put(self, connector_name: str, state: ConnectorState) -> None:
        item: dict[str, str] = {"connector_name": connector_name}
        if state.etag is not None:
            item["etag"] = state.etag
        if state.last_modified is not None:
            item["last_modified"] = state.last_modified
        if state.payload is not None:
            item["payload"] = json.dumps(state.payload)
        item["last_fetched_at"] = datetime.now(timezone.utc).isoformat()
        self._table.put_item(Item=item)


@dataclass(frozen=True)
class DiscoveredSlug:
    connector_name: str       # "{ats}:{slug}"
    ats_family: str
    slug: str
    company_name: str | None
    discovered_at: str
    last_validated_at: str | None
    validation_status: str    # "ok" | "failed" | "quarantined" | "no_match" | "candidate"
    consecutive_failures: int
    last_posting_count: int
    origin: str | None = None          # "hiringcafe" | "vc:{firm}" | None (legacy/yc-oss)
    sighted_at: str | None = None      # ISO — first sighting (candidate staging)
    claimed_family: str | None = None  # candidate: the family the source asserted
    website: str | None = None               # no_match learning (conversion chain)
    methods_tried: list[str] | None = None   # e.g. ["slug:acme", "domain:acme", "fingerprint"]


class DiscoveredSlugsStore:
    """Persists slugs discovered by the discovery routine.

    Runtime poll list = config.yaml seeded ∪ rows here with validation_status='ok'."""

    def __init__(self, table_name: str, *, region: str = "us-east-1") -> None:
        self._table_name = table_name
        self._ddb = boto3.resource("dynamodb", region_name=region)
        self._table = self._ddb.Table(table_name)

    def get(self, connector_name: str) -> DiscoveredSlug | None:
        resp = self._table.get_item(Key={"connector_name": connector_name})
        item = resp.get("Item")
        if not item:
            return None
        return _row_from_item(item)

    def list_healthy(self) -> list[DiscoveredSlug]:
        return [r for r in self.list_all() if r.validation_status == "ok"]

    def list_all(self) -> list[DiscoveredSlug]:
        rows: list[DiscoveredSlug] = []
        params: dict = {}
        while True:
            resp = self._table.scan(**params) if params else self._table.scan()
            for item in resp.get("Items", []):
                rows.append(_row_from_item(item))
            last = resp.get("LastEvaluatedKey")
            if not last:
                break
            params = {"ExclusiveStartKey": last}
        return rows

    def upsert_ok(
        self,
        connector_name: str,
        *,
        company_name: str | None = None,
        last_posting_count: int = 0,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        ats_family, slug = connector_name.split(":", 1)
        existing = self.get(connector_name)
        item = {
            "connector_name": connector_name,
            "ats_family": ats_family,
            "slug": slug,
            "company_name": company_name or (existing.company_name if existing else None),
            "discovered_at": existing.discovered_at if existing else now,
            "last_validated_at": now,
            "validation_status": "ok",
            "consecutive_failures": 0,
            "last_posting_count": last_posting_count,
        }
        self._table.put_item(Item={k: v for k, v in item.items() if v is not None})

    def upsert_failed(
        self,
        connector_name: str,
        *,
        quarantine_threshold: int = 5,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        ats_family, slug = connector_name.split(":", 1)
        existing = self.get(connector_name)
        failures = (existing.consecutive_failures if existing else 0) + 1
        status = "quarantined" if failures >= quarantine_threshold else "failed"
        item: dict[str, object] = {
            "connector_name": connector_name,
            "ats_family": ats_family,
            "slug": slug,
            "company_name": existing.company_name if existing else None,
            "discovered_at": existing.discovered_at if existing else now,
            "last_validated_at": now,
            "validation_status": status,
            "consecutive_failures": failures,
            "last_posting_count": existing.last_posting_count if existing else 0,
        }
        self._table.put_item(Item={k: v for k, v in item.items() if v is not None})

    def upsert_no_match(
        self,
        slug: str,
        *,
        company_name: str | None = None,
        website: str | None = None,
        methods_tried: list[str] | None = None,
    ) -> None:
        """Record that a slug's conversion chain fully missed. Chain metadata
        (name/website/methods_tried) defaults to the existing row's values so
        a legacy plain call never clobbers learned fields. Used to suppress
        re-probing for ``no_match_revalidate_after_days``."""
        now = datetime.now(timezone.utc).isoformat()
        connector_name = f"nomatch:{slug}"
        existing = self.get(connector_name)
        item = {
            "connector_name": connector_name,
            "ats_family": "nomatch",
            "slug": slug,
            "company_name": company_name or (existing.company_name if existing else None),
            "discovered_at": existing.discovered_at if existing else now,
            "last_validated_at": now,
            "validation_status": "no_match",
            "consecutive_failures": 0,
            "last_posting_count": 0,
            "website": website or (existing.website if existing else None),
            "methods_tried": methods_tried if methods_tried is not None
                             else (existing.methods_tried if existing else None),
        }
        self._table.put_item(Item={k: v for k, v in item.items() if v is not None})

    def upsert_candidate(self, connector_name: str, *, company_name: str | None = None,
                         origin: str | None = None, claimed_family: str | None = None,
                         website: str | None = None) -> None:
        """Stage a sighted-but-unprobed candidate. No-op if ANY row already
        exists under this key — never demote a validated/failed/no_match row."""
        if self.get(connector_name) is not None:
            return
        now = datetime.now(timezone.utc).isoformat()
        ats_family, slug = connector_name.split(":", 1)
        item = {
            "connector_name": connector_name, "ats_family": ats_family, "slug": slug,
            "company_name": company_name, "discovered_at": now, "sighted_at": now,
            "validation_status": "candidate", "consecutive_failures": 0,
            "last_posting_count": 0, "origin": origin, "claimed_family": claimed_family,
            "website": website,
        }
        self._table.put_item(Item={k: v for k, v in item.items() if v is not None})

    def list_candidates(self) -> list[DiscoveredSlug]:
        return [r for r in self.list_all() if r.validation_status == "candidate"]

    def list_no_match(self) -> list[DiscoveredSlug]:
        return [r for r in self.list_all() if r.validation_status == "no_match"]

    def delete(self, connector_name: str) -> None:
        self._table.delete_item(Key={"connector_name": connector_name})

    def resolve_candidate_no_match(self, connector_name: str) -> None:
        """A drained candidate that fully missed: overwrite IN PLACE to no_match
        so it leaves the drain queue while capture-dedup still sees the key."""
        row = self.get(connector_name)
        if row is None or row.validation_status != "candidate":
            return
        now = datetime.now(timezone.utc).isoformat()
        item = {
            "connector_name": row.connector_name, "ats_family": row.ats_family,
            "slug": row.slug, "company_name": row.company_name,
            "discovered_at": row.discovered_at, "last_validated_at": now,
            "validation_status": "no_match", "consecutive_failures": 0,
            "last_posting_count": 0, "origin": row.origin,
            "sighted_at": row.sighted_at, "claimed_family": row.claimed_family,
        }
        self._table.put_item(Item={k: v for k, v in item.items() if v is not None})

    def is_recent_no_match(self, slug: str, *, fresh_within_days: int) -> bool:
        """True iff a no_match row exists for this slug AND its
        last_validated_at is within fresh_within_days of now."""
        resp = self._table.get_item(Key={"connector_name": f"nomatch:{slug}"})
        item = resp.get("Item")
        if not item:
            return False
        last = item.get("last_validated_at")
        if not last:
            return False
        try:
            dt = datetime.fromisoformat(last)
        except ValueError:
            return False
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt) < timedelta(days=fresh_within_days)

    def list_for_revalidation(
        self,
        *,
        stale_after_days: int,
        limit: int,
    ) -> list[DiscoveredSlug]:
        """Return up to ``limit`` ok rows whose last_validated_at is older than
        ``stale_after_days`` days ago. Ordering is the scan order (effectively
        random); callers should not depend on it."""
        threshold = datetime.now(timezone.utc) - timedelta(days=stale_after_days)
        out: list[DiscoveredSlug] = []
        for row in self.list_all():
            if row.validation_status != "ok":
                continue
            if not row.last_validated_at:
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


def _row_from_item(item: dict) -> DiscoveredSlug:
    return DiscoveredSlug(
        connector_name=item["connector_name"],
        ats_family=item.get("ats_family", item["connector_name"].split(":", 1)[0]),
        slug=item.get("slug", item["connector_name"].split(":", 1)[1]),
        company_name=item.get("company_name"),
        discovered_at=item.get("discovered_at", ""),
        last_validated_at=item.get("last_validated_at"),
        validation_status=item.get("validation_status", "ok"),
        consecutive_failures=int(item.get("consecutive_failures", 0)),
        last_posting_count=int(item.get("last_posting_count", 0)),
        origin=item.get("origin"),
        sighted_at=item.get("sighted_at"),
        claimed_family=item.get("claimed_family"),
        website=item.get("website"),
        methods_tried=item.get("methods_tried"),
    )


def no_match_exhausted(row: DiscoveredSlug) -> bool:
    """True iff the row's conversion chain completed with nothing left to
    try: every variant missed and — when the row has a website — the
    fingerprint fallback definitively missed too (a transient fingerprint
    error never appends 'fingerprint', keeping the row non-exhausted).
    Legacy rows (no methods_tried) are non-exhausted so their first
    post-deploy expiry runs the full chain."""
    methods = row.methods_tried or []
    if not methods:
        return False
    if row.website:
        return "fingerprint" in methods
    return True


@dataclass(frozen=True)
class DiscoveredBoard:
    domain: str                 # seed domain — primary key / candidate id
    name: str                   # seed company name
    status: str                 # "ok" | "not_found" | "unsupported" | "error" | "quarantined"
    family: str | None          # match only
    identity: dict | None       # match only — the FingerprintResult.identity
    connector_name: str | None  # match only — for dedup
    company: str | None
    last_swept_at: str
    failure_streak: int
    origin: str | None = None      # "hiringcafe" | None (seed-CSV sweep)
    sighted_at: str | None = None  # ISO — first sighting (candidate staging)


def _board_from_item(item: dict) -> DiscoveredBoard:
    return DiscoveredBoard(
        domain=item["domain"],
        name=item.get("name") or item["domain"],
        status=item["status"],
        family=item.get("family"),
        identity=item.get("identity"),
        connector_name=item.get("connector_name"),
        company=item.get("company"),
        last_swept_at=item.get("last_swept_at") or "",
        failure_streak=item.get("failure_streak", 0),
        origin=item.get("origin"),
        sighted_at=item.get("sighted_at"),
    )


class ConnectorHealthStore:
    """Per-connector poll health for the circuit breaker. Sparse: a row exists
    only while a connector is unhealthy (dead_streak > 0 or suppressed). Keyed by
    ``connector_name`` — the same identity used in build_connectors and
    failed_sources (e.g. 'greenhouse:acme', 'workday:walmart:WalmartExternal')."""

    def __init__(self, table_name: str, *, region: str = "us-east-1") -> None:
        self._table_name = table_name
        self._ddb = boto3.resource("dynamodb", region_name=region)
        self._table = self._ddb.Table(table_name)

    def _scan_names(self, *, suppressed_only: bool) -> set[str]:
        names: set[str] = set()
        params: dict = {"ProjectionExpression": "connector_name"}
        if suppressed_only:
            params["FilterExpression"] = "#s = :true"
            params["ExpressionAttributeNames"] = {"#s": "suppressed"}
            params["ExpressionAttributeValues"] = {":true": True}
        while True:
            resp = self._table.scan(**params)
            for item in resp.get("Items", []):
                names.add(item["connector_name"])
            last = resp.get("LastEvaluatedKey")
            if not last:
                break
            params["ExclusiveStartKey"] = last
        return names

    def suppressed_names(self) -> set[str]:
        """Connectors currently tripped (suppressed=true). Polled connectors are
        filtered against this set in build_connectors."""
        return self._scan_names(suppressed_only=True)

    def tracked_names(self) -> set[str]:
        """Every connector that has a health row (suppressed OR mid-streak). Read
        once per cycle so the 'ok' path can be write-on-change."""
        return self._scan_names(suppressed_only=False)

    def record_dead(self, connector_name: str) -> int:
        """Atomically increment the consecutive-dead streak (creating the row if
        absent) and return the new value. Atomic increment avoids a
        read-modify-write race between concurrent ats invocations."""
        now = datetime.now(timezone.utc).isoformat()
        resp = self._table.update_item(
            Key={"connector_name": connector_name},
            UpdateExpression="SET dead_streak = if_not_exists(dead_streak, :zero) + :one, updated_at = :now",
            ExpressionAttributeValues={":zero": 0, ":one": 1, ":now": now},
            ReturnValues="UPDATED_NEW",
        )
        return int(resp["Attributes"]["dead_streak"])

    def mark_suppressed(self, connector_name: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self._table.update_item(
            Key={"connector_name": connector_name},
            UpdateExpression="SET #s = :true, updated_at = :now",
            ExpressionAttributeNames={"#s": "suppressed"},
            ExpressionAttributeValues={":true": True, ":now": now},
        )

    def clear(self, connector_name: str) -> None:
        """Delete the row (back to implicit-healthy). Idempotent — a delete of a
        missing key is a no-op in DynamoDB."""
        self._table.delete_item(Key={"connector_name": connector_name})

    def mark_backoff(self, connector_name: str, until_ms: int) -> None:
        """Temporarily skip a connector until `until_ms` (epoch ms) — a soft,
        self-expiring pause for rate-limited (429) hosts. Separate from the
        permanent dead-suppression path; leaves dead_streak/suppressed untouched."""
        now = datetime.now(timezone.utc).isoformat()
        self._table.update_item(
            Key={"connector_name": connector_name},
            UpdateExpression="SET backoff_until = :until, updated_at = :now",
            ExpressionAttributeValues={":until": int(until_ms), ":now": now},
        )

    def backoff_names(self, now_ms: int) -> set[str]:
        """Connectors whose backoff window hasn't expired yet (skip this cycle)."""
        names: set[str] = set()
        params: dict = {
            "ProjectionExpression": "connector_name",
            "FilterExpression": "backoff_until > :now",
            "ExpressionAttributeValues": {":now": int(now_ms)},
        }
        while True:
            resp = self._table.scan(**params)
            for item in resp.get("Items", []):
                names.add(item["connector_name"])
            last = resp.get("LastEvaluatedKey")
            if not last:
                break
            params["ExclusiveStartKey"] = last
        return names
