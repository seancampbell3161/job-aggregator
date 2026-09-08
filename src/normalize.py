from __future__ import annotations

import logging
import re

from src import geo
from src.models import NormalizedPosting, RawPosting
from src.sanitize import sanitize_description

log = logging.getLogger(__name__)

_SENIORITY_PATTERNS = [
    # Negative lookbehind excludes "Member of Technical Staff" (a base IC title,
    # not a staff-level role) from the "staff" seniority bucket. "technical\s" is
    # fixed-width, so it's valid inside a Python lookbehind.
    ("staff", re.compile(r"\b((?<!technical\s)staff|principal|distinguished|fellow)\b", re.I)),
    ("manager", re.compile(r"\b(manager|director|vp|head of|chief|architect)\b", re.I)),
    ("senior", re.compile(r"\b(senior|sr\.?|lead)\b", re.I)),
    ("junior", re.compile(r"\b(junior|jr\.?|entry[-\s]?level|new grad|associate)\b", re.I)),
]

# "Architect" is grouped with manager because role expectations diverge from IC SWE.
# Plain "Software Engineer" returns None — the filter treats None as "mid".

_LOC_REMOTE = re.compile(r"\bremote\b", re.I)
_LOC_HYBRID = re.compile(r"\bhybrid\b", re.I)
_LOC_ONSITE = re.compile(r"\bon[-\s]?site\b|\bin[-\s]?office\b", re.I)

# Canonical employment types. None means "unknown" — the employment_type filter
# fails open on None so recall is unaffected when a connector omits the signal.
# Keep these in sync with config.EmploymentType.
EMPLOYMENT_TYPES = frozenset(
    {"full_time", "part_time", "contract", "contract_to_hire", "temporary", "internship"}
)


def normalize_employment_type(raw: str | None) -> str | None:
    """Map a connector's raw commitment/employment string onto a canonical value.

    Returns None for empty input or anything we can't confidently classify —
    callers treat None as "unknown" and never reject on it. Contract-to-hire is
    classified distinctly from pure contract because the profile accepts it."""
    if not raw:
        return None
    s = re.sub(r"[\s._/|-]+", " ", raw.strip().lower()).strip()
    if not s:
        return None
    # Contract-to-hire (a.k.a. temp-to-perm) is acceptable — classify before the
    # bare "contract"/"temp" checks below so it isn't swept into those buckets.
    if ("contract" in s or "temp" in s) and (
        "to hire" in s or "to perm" in s or "c2h" in s or "then perm" in s
    ):
        return "contract_to_hire"
    if "intern" in s:
        return "internship"
    if any(t in s for t in ("contract", "1099", "c2c", "corp to corp", "freelance", "consult")):
        return "contract"
    if "temp" in s or "seasonal" in s:
        return "temporary"
    if "part" in s:  # "part time", "part-time"
        return "part_time"
    if "full" in s or "permanent" in s or s in ("perm", "regular"):
        return "full_time"
    return None


def _stack_pattern(keyword: str) -> re.Pattern[str]:
    kw = keyword.lower()
    if kw == "go":
        return re.compile(r"\bGo\b|\bGolang\b|\bGo developer\b", re.I)
    if kw == "c#":
        return re.compile(r"\bC#", re.I)
    if kw == ".net":
        return re.compile(r"\.NET\b", re.I)
    if kw == "next.js":
        return re.compile(r"\bNext\.?js\b", re.I)
    return re.compile(rf"\b{re.escape(kw)}\b", re.I)


def _seniority(title: str | None) -> str | None:
    if not title:
        return None
    for label, pat in _SENIORITY_PATTERNS:
        if pat.search(title):
            return label
    return None


def _location_tags(
    location: str | None, remote: bool | None, allowed_cities: list[str]
) -> set[str]:
    tags: set[str] = set()
    if remote is True:
        tags.add("remote")
    if location:
        if _LOC_REMOTE.search(location):
            tags.add("remote")
        if _LOC_HYBRID.search(location):
            tags.add("hybrid")
        if _LOC_ONSITE.search(location):
            tags.add("onsite_only")
        if not tags:
            # Has a location string but no remote/hybrid/onsite marker. Ashby returns
            # isRemote=None (not False) for non-remote postings, so the only way to
            # know "this isn't remote" is the absence of remote signal in the location
            # text — treat that as onsite_only. Connectors that explicitly say
            # remote=True already added the "remote" tag above.
            tags.add("onsite_only")

        # City tags: check each allowed city for a word-boundary match.
        for city in allowed_cities:
            pat = re.compile(rf"\b{re.escape(city)}\b", re.I)
            if pat.search(location):
                tags.add(city.lower())

        # Geographic signals: country:xx / region:yy (see src/geo.py).
        tags |= geo.resolve_geo_tags(location)
    else:
        if remote is None:
            tags.add("unknown_location")
        elif remote is False:
            tags.add("onsite_only")
    return tags


def workplace_type_from_tags(location_tags: frozenset[str] | set[str]) -> str | None:
    """Collapse the normalizer's location_tags into a single workplace bucket for
    the triage filter, or None when the set carries no workplace signal (so the
    caller can fall back to a text-based guess). This is the authoritative path:
    location_tags already folds in the connector remote flag and the ATS workplace
    enum. A role tagged both remote and hybrid resolves to hybrid — the more
    specific arrangement, matching workplace_type_of's priority."""
    if "hybrid" in location_tags:
        return "hybrid"
    if "remote" in location_tags:
        return "remote"
    if "onsite_only" in location_tags:
        return "onsite"
    if "unknown_location" in location_tags:
        return "unknown"
    return None


def workplace_type_of(location_text: str) -> str:
    """Best-effort workplace bucket from free-text location alone — the fallback
    for rows persisted before workplace_type was stored. Less reliable than
    workplace_type_from_tags: the connector remote flag isn't recoverable from
    text, so a flag-only remote role with a bare/empty location reads as
    onsite/unknown. Same hybrid > remote > onsite priority; empty → unknown."""
    loc = (location_text or "").strip()
    if not loc:
        return "unknown"
    if _LOC_HYBRID.search(loc):
        return "hybrid"
    if _LOC_REMOTE.search(loc):
        return "remote"
    return "onsite"


def _extract_stack(text: str, keywords: list[str]) -> set[str]:
    found: set[str] = set()
    for kw in keywords:
        if _stack_pattern(kw).search(text):
            found.add(kw.lower())
    return found


# Connectors that wrap another ATS: "{connector}:{ats_family}:{slug}", where the
# employer is the trailing slug and the middle segment names the ATS vendor.
# Everywhere else the second segment carries the name — either the whole slug
# ("greenhouse:stripe") or a tenant followed by a career-site id
# ("workday:paypal:jobs", "oraclecloud:egug:CX_1").
_NESTED_ATS_SOURCES = frozenset({"hiringcafe"})


def _company(source: str, title: str) -> str:
    """Best-effort display name derived from the source string.

    A fallback only: connectors that know the employer set RawPosting.company and
    never land here. Which segment carries the name depends on the source shape —
    "hiringcafe:ashby:mercor" is Mercor, not Ashby — and reading the wrong one
    files unrelated employers under their ATS vendor's name, which also defeats
    any per-company grouping or cap built on top of this value."""
    if source.startswith("hn:"):
        # HN title: "Company | Role"
        return title.split("|", 1)[0].strip() or "unknown"
    parts = source.split(":")
    if len(parts) < 2:
        return source
    # "greenhouse:stripe" → "Stripe"; "hiringcafe:ashby:mercor" → "Mercor"
    slug = parts[-1] if len(parts) > 2 and parts[0] in _NESTED_ATS_SOURCES else parts[1]
    return slug.replace("-", " ").replace("_", " ").title()


def normalize(
    raw: RawPosting,
    *,
    stack_keywords: list[str],
    allowed_cities: list[str] | None = None,
) -> NormalizedPosting:
    # Defang before anything else reads the description: stack extraction, the
    # relevance prompt, and the stored snapshot (which the tailor endpoint and
    # the audit page later re-read) all derive from this one value.
    description, fired = sanitize_description(raw.description)
    if fired:
        log.warning(
            "posting_injection_filtered",
            extra={"job_id": f"{raw.source}:{raw.external_id}",
                   "source": raw.source, "rules": ",".join(fired)},
        )
    # RawPosting is an unvalidated dataclass, so a connector that passes an
    # upstream JSON null lands here as None despite the `str` annotation. Coerce
    # once, up front, rather than making every downstream helper null-aware.
    title = raw.title or ""
    text = f"{title}\n{description}"
    cities = allowed_cities if allowed_cities is not None else []
    return NormalizedPosting(
        job_id=f"{raw.source}:{raw.external_id}",
        title=title,
        company=raw.company or _company(raw.source, title),
        location_text=raw.location or "",
        location_tags=frozenset(_location_tags(raw.location, raw.remote, cities)),
        seniority=_seniority(title),
        stack=frozenset(_extract_stack(text, stack_keywords)),
        comp_min=raw.comp_min,
        comp_max=raw.comp_max,
        apply_url=raw.apply_url,
        description=description,
        posted_at=raw.posted_at,
        source=raw.source,
        employment_type=normalize_employment_type(raw.employment_type),
    )
