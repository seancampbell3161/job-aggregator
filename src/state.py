"""Shared row types and item-shaping helpers for the SQLite stores in
src/state_sqlite.py: status vocabularies, the persisted posting fields, the
triage view shape, and the discovered-slug / discovered-board row types."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from src.models import NormalizedPosting
from src.normalize import workplace_type_from_tags
from src.sanitize import sanitize_description

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
