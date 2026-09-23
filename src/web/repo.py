from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from src.models import NormalizedPosting

# workplace_type_of is re-exported here (imported by tests via src.web.repo) and
# used as the fallback when a row predates persisted workplace_type. It lives in
# normalize alongside the tag vocabulary it mirrors, so the two can't drift.
from src.normalize import workplace_type_of
from src.slugging import normalize_name_slug
from src.state import VALID_STATUSES
from src.state_sqlite import SqliteSeenJobsStore

# Source family for opportunities typed in by hand (a recruiter DM, a referral).
# Connector sources are "{ats}:{slug}", so reserving a family keeps hand-entered
# rows from ever colliding with a real board and makes analytics bucket them on
# their own (rank_by_ats splits the source on ':').
MANUAL_SOURCE = "manual"
_ID_UNSAFE_RE = re.compile(r"[^a-z0-9-]")


@dataclass(frozen=True)
class TriageMatch:
    job_id: str
    title: str
    company: str
    location_text: str
    comp_min: int | None
    comp_max: int | None
    apply_url: str
    source: str
    posted_at: str | None
    score: int | None
    rationale: str | None
    gaps: list[str]
    first_seen: str
    status: str
    history: list[dict] = field(default_factory=list)
    posting_closed_at: str | None = None
    email_suggestion: dict | None = None
    # Authoritative workplace bucket persisted by the pipeline (from location_tags);
    # None for rows stored before it was persisted — see the workplace_type property.
    workplace_type_stored: str | None = None

    @property
    def current_since(self) -> str:
        """ISO date (YYYY-MM-DD) the row entered its current status; falls back to
        first_seen when no history has accumulated yet (pre-migration rows)."""
        if self.history:
            at = self.history[-1].get("at", "")
            if at:
                return at[:10]
        return (self.first_seen or "")[:10]

    @property
    def days_in_stage(self) -> int:
        try:
            since = date.fromisoformat(self.current_since)
        except (ValueError, TypeError):
            return 0
        return (datetime.now(timezone.utc).date() - since).days

    @property
    def stage_timeline(self) -> list[tuple[str, str]]:
        """(status, 'M/D') pairs for the card's mini timeline, oldest first."""
        out: list[tuple[str, str]] = []
        for e in self.history:
            at = (e.get("at") or "")[:10]
            label = at
            try:
                d = date.fromisoformat(at)
                label = f"{d.month}/{d.day}"
            except (ValueError, TypeError):
                pass
            out.append((e.get("status", ""), label))
        return out

    def is_stale(self, threshold: int) -> bool:
        return self.status in ("applied", "interviewing") and self.days_in_stage >= threshold

    def staleness_label(self, threshold: int) -> str:
        return f"{self.days_in_stage}d · no movement" if self.is_stale(threshold) else ""

    def closed_label(self) -> str:
        """'posting closed 3d ago' when the daily check has confirmed closure;
        '' otherwise. Day count from the flag timestamp's date."""
        if not self.posting_closed_at:
            return ""
        try:
            closed = date.fromisoformat(self.posting_closed_at[:10])
        except (ValueError, TypeError):
            return "posting closed"
        days = (datetime.now(timezone.utc).date() - closed).days
        return "posting closed today" if days <= 0 else f"posting closed {days}d ago"

    def suggestion_label(self) -> str:
        """"✉ looks rejected" / "✉ receipt: applied?" from the gmail-ingest
        suggestion, '' when none."""
        s = self.email_suggestion or {}
        status = s.get("suggested_status")
        if status == "rejected":
            return "✉ looks rejected"
        if status == "applied":
            return "✉ receipt: applied?"
        return ""

    @property
    def is_manual(self) -> bool:
        """True for a row typed in by hand rather than polled from a board."""
        return self.source == MANUAL_SOURCE or self.source.startswith(f"{MANUAL_SOURCE}:")

    @property
    def workplace_type(self) -> str:
        """remote / hybrid / onsite / unknown. Prefers the value the pipeline
        persisted from location_tags (which captures the remote flag + ATS enum);
        falls back to a text-only guess for rows stored before it was persisted."""
        return self.workplace_type_stored or workplace_type_of(self.location_text)

    @property
    def comp_display(self) -> str:
        if self.comp_min is not None and self.comp_max is not None:
            return f"${self.comp_min // 1000}k–${self.comp_max // 1000}k"
        if self.comp_min is not None:
            return f"${self.comp_min // 1000}k+"
        return ""

    @property
    def date_display(self) -> str:
        raw = self.posted_at or self.first_seen or ""
        return raw[:10]  # ISO date prefix

    @property
    def status_css(self) -> str:
        """The status as a known CSS-class token. Defense-in-depth: status is
        validated on write by set_status, but this guarantees the template can
        never emit an arbitrary class even if a row was written out-of-band."""
        return self.status if self.status in VALID_STATUSES else "new"

    @property
    def apply_href(self) -> str:
        """apply_url restricted to http(s), so a malformed or rogue posting can't
        slip a javascript: (or other) URI into the Apply link. Falls back to '#'."""
        url = self.apply_url or ""
        return url if url.startswith(("https://", "http://")) else "#"

    def band(self, score_high: int, score_low: int) -> str:
        if self.score is None:
            return "none"
        if self.score >= score_high:
            return "high"
        if self.score <= score_low:
            return "low"
        return "mid"


def _from_row(row: dict) -> TriageMatch:
    return TriageMatch(
        job_id=row["job_id"], title=row["title"], company=row["company"],
        location_text=row["location_text"], comp_min=row["comp_min"],
        comp_max=row["comp_max"], apply_url=row["apply_url"], source=row["source"],
        posted_at=row["posted_at"], score=row["score"], rationale=row["rationale"],
        gaps=row["gaps"], first_seen=row["first_seen"], status=row["status"],
        history=row.get("history", []),
        posting_closed_at=row.get("posting_closed_at"),
        email_suggestion=row.get("email_suggestion"),
        workplace_type_stored=row.get("workplace_type"),
    )


def filter_sort_search(
    matches: list[TriageMatch],
    *,
    statuses: set[str] | None = None,
    min_score: int | None = None,
    has_gaps: bool = False,
    workplace: set[str] | None = None,
    query: str = "",
    sort: str = "score",
) -> list[TriageMatch]:
    out = list(matches)
    if statuses:
        out = [m for m in out if m.status in statuses]
    if min_score is not None:
        out = [m for m in out if m.score is not None and m.score >= min_score]
    if has_gaps:
        out = [m for m in out if m.gaps]
    if workplace:
        out = [m for m in out if m.workplace_type in workplace]
    if query:
        q = query.lower()
        out = [m for m in out if q in m.title.lower() or q in m.company.lower()]
    if sort == "newest":
        out.sort(key=lambda m: m.first_seen, reverse=True)
    else:  # "score": scored first (desc), unscored last; newest as tiebreak
        out.sort(key=lambda m: (m.score if m.score is not None else -1, m.first_seen), reverse=True)
    return out


def manual_job_id(company: str, title: str, *, now: datetime, attempt: int = 0) -> str:
    """Deterministic id for a hand-entered opportunity, in the connector's own
    "{source}:{external_id}" shape so every downstream reader treats it like any
    other row. The second-resolution stamp makes two adds of the same role at
    different times distinct rows; `attempt` disambiguates the same-second case.

    The slug is narrowed to [a-z0-9-] on top of normalize_name_slug (which is
    shared with ATS discovery and leaves ':' and non-ASCII alone) so a typed
    company name can't put separators or unicode into a store key."""
    slug = _ID_UNSAFE_RE.sub("", normalize_name_slug(f"{company} {title}")).strip("-")[:60]
    tail = f"-{attempt}" if attempt else ""
    return f"{MANUAL_SOURCE}:{slug or 'job'}:{now.strftime('%Y%m%d%H%M%S')}{tail}"


class TriageRepo:
    """Store-backed adapter: reads rows, shapes them into TriageMatch view
    models, applies in-memory filter/sort/search, and writes status."""

    def __init__(self, store: SqliteSeenJobsStore) -> None:
        self._store = store

    def list(self, **filters) -> list[TriageMatch]:
        matches = [_from_row(r) for r in self._store.list_matches()]
        return filter_sort_search(matches, **filters)

    def status_counts(self) -> dict[str, int]:
        """Matches per status, counted by the store (no row load)."""
        return self._store.match_status_counts()

    def get(self, job_id: str) -> TriageMatch | None:
        row = self._store.get_match(job_id)
        return _from_row(row) if row is not None else None

    def set_status(self, job_id: str, status: str) -> bool:
        return self._store.set_status(job_id, status)

    def add_manual(
        self,
        *,
        company: str,
        title: str,
        status: str = "interested",
        location_text: str = "",
        apply_url: str = "",
        comp_min: int | None = None,
        comp_max: int | None = None,
        channel: str = "",
        description: str = "",
        now: datetime | None = None,
    ) -> str:
        """Write a hand-entered opportunity as a notified match and move it to
        `status`. Returns the new job_id.

        Built on claim_for_notify + set_status rather than a new store method so
        both backends behave identically and the row is indistinguishable from a
        polled one everywhere downstream (triage, board, funnel, analytics). It
        carries no relevance score — nothing scored it — so the UI shows '–'.
        set_status also clears the 60-day TTL for the pursued statuses, which is
        what keeps a hand-added row from quietly expiring.

        Raises ValueError on a blank company/title or an unknown status.
        """
        company, title = company.strip(), title.strip()
        if not company or not title:
            raise ValueError("company and title are required")
        if status not in VALID_STATUSES:
            raise ValueError(f"invalid status: {status!r}")
        now = now or datetime.now(timezone.utc)
        # source stays "manual[:channel]" so rank_by_ats — which splits on the
        # first ':' — always buckets these as one ATS named "manual".
        channel_slug = _ID_UNSAFE_RE.sub("", normalize_name_slug(channel)).strip("-")[:40]
        source = f"{MANUAL_SOURCE}:{channel_slug}" if channel_slug else MANUAL_SOURCE

        for attempt in range(5):
            job_id = manual_job_id(company, title, now=now, attempt=attempt)
            posting = NormalizedPosting(
                job_id=job_id, title=title, company=company,
                location_text=location_text.strip(), location_tags=frozenset(),
                seniority=None, stack=frozenset(),
                comp_min=comp_min, comp_max=comp_max,
                apply_url=apply_url.strip(), description=description.strip(),
                # No posted_at: we know when the lead arrived, not when the role
                # went live. date_display then falls back to first_seen.
                posted_at=None, source=source,
            )
            claimed = self._store.claim_for_notify(
                job_id,
                rationale=f"Added by hand from {channel.strip() or 'an off-board lead'}.",
                posting=posting,
            )
            if claimed:
                self._store.set_status(job_id, status)
                return job_id
        raise ValueError("could not allocate a job id for this entry")
