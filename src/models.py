from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

Tier = Literal["ats", "slow", "discovery", "headless"]


@dataclass(frozen=True)
class RawPosting:
    """What a connector returns. Unnormalized."""
    source: str           # "greenhouse:stripe"
    external_id: str
    title: str
    description: str
    apply_url: str
    location: str | None = None
    remote: bool | None = None
    department: str | None = None
    company: str | None = None
    posted_at: datetime | None = None
    comp_min: int | None = None
    comp_max: int | None = None
    employment_type: str | None = None  # raw ATS commitment string, e.g. "Full-time", "Contract"
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class NormalizedPosting:
    job_id: str           # "{source}:{external_id}"
    title: str
    company: str
    location_text: str
    location_tags: frozenset[str]
    seniority: str | None  # "junior" | "mid" | "senior" | "staff" | "manager" | None
    stack: frozenset[str]
    comp_min: int | None
    comp_max: int | None
    apply_url: str
    description: str
    posted_at: datetime | None
    source: str
    # Canonical employment type (see normalize.normalize_employment_type). None =
    # unknown; the employment_type filter fails open on None to protect recall.
    employment_type: str | None = None


@dataclass(frozen=True)
class ConnectorState:
    """Persisted per-connector state: HTTP cache hints plus an optional
    JSON-serializable payload for connectors that need richer persistence
    (e.g. adzuna's daily call budget + seen-ID FIFO)."""
    etag: str | None = None
    last_modified: str | None = None
    payload: dict | None = None


@dataclass(frozen=True)
class FetchResult:
    """What a connector returns. `new_state=None` means 'leave persisted state unchanged'
    (e.g. on a 304 response, on connectors that don't support conditional GET, or on
    connectors that errored before a 200)."""
    postings: list[RawPosting]
    new_state: ConnectorState | None = None
    not_modified: bool = False
