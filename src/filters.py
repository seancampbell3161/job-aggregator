from __future__ import annotations

import enum
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache

from src import geo
from src.config import FiltersConfig
from src.models import NormalizedPosting


class Verdict(enum.Enum):
    MATCH = "match"
    REJECT = "reject"
    UNKNOWN = "unknown"


@dataclass
class Decision:
    allow: bool
    rejected_by: str | None = None
    unknowns: list[str] = field(default_factory=list)


@lru_cache(maxsize=8)
def _build_role_regex(titles: tuple[str, ...]) -> re.Pattern[str]:
    """Compile a word-boundary alternation from the configured title list.

    Each title is escaped and joined with `|`. A title like "full-stack engineer"
    matches that exact string (case-insensitive) on a word boundary; the user
    adds explicit variants to config rather than embedding regex syntax."""
    if not titles:
        return re.compile(r"$^")  # never matches
    parts = "|".join(re.escape(t) for t in titles)
    return re.compile(rf"\b({parts})\b", re.IGNORECASE)


def filter_role(p: NormalizedPosting, f: FiltersConfig) -> Verdict:
    pat = _build_role_regex(tuple(f.titles))
    return Verdict.MATCH if pat.search(p.title) else Verdict.REJECT


_COMPANY_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _company_tokens(name: str) -> tuple[str, ...]:
    return tuple(_COMPANY_TOKEN_RE.findall(name.lower()))


@lru_cache(maxsize=8)
def _blocked_company_tokens(names: tuple[str, ...]) -> tuple[tuple[str, ...], ...]:
    """Tokenized denylist entries; blank entries drop out (they'd match everything)."""
    return tuple(t for t in (_company_tokens(n) for n in names) if t)


def filter_company(p: NormalizedPosting, f: FiltersConfig) -> Verdict:
    """Reject a posting whose company is on the denylist.

    Matches an entry's words consecutively against the company's words. Not a
    substring test: connectors that expose no company fall back to the board
    slug (`normalize._company` yields "Paypal:Jobs", "Eightfold:Microsoft"), so
    tokenizing on non-alphanumerics is what makes both spellings of a name
    resolve alike — while keeping "apple" off "Applebee's"."""
    blocked = _blocked_company_tokens(tuple(f.blocked_companies))
    if not blocked:
        return Verdict.MATCH
    toks = _company_tokens(p.company or "")
    for entry in blocked:
        n = len(entry)
        if any(toks[i : i + n] == entry for i in range(len(toks) - n + 1)):
            return Verdict.REJECT
    return Verdict.MATCH


def filter_employment_type(p: NormalizedPosting, f: FiltersConfig) -> Verdict:
    # Fail open on unknown: most connectors don't expose employment type, and we
    # never want to drop a genuine full-time role just because the signal is
    # missing. Only a *known* blocked type (e.g. contract) is rejected.
    et = p.employment_type
    if et is None:
        return Verdict.UNKNOWN
    return Verdict.REJECT if et in f.blocked_employment_types else Verdict.MATCH


def filter_seniority(p: NormalizedPosting, f: FiltersConfig) -> Verdict:
    # None → treat as "mid" (plain "Software Engineer")
    s = p.seniority or "mid"
    if s in f.seniority_allow:
        return Verdict.MATCH
    return Verdict.REJECT


def filter_location(p: NormalizedPosting, f: FiltersConfig) -> Verdict:
    tags = p.location_tags
    loc = f.location
    allowed = {c.lower() for c in loc.allowed_countries}
    geo_sig = geo.geo_tags(tags)

    # Step 1: posting is in an allowed city → MATCH, unless the location text
    # resolves to a geography that doesn't cover any allowed country. A local
    # city name can collide with a foreign one (e.g. "San Jose, Costa Rica",
    # "Portland, Australia") — the resolved foreign country disqualifies the
    # city match.
    allowed_city_set = {c.lower() for c in loc.allowed_cities}
    if (tags & allowed_city_set) and (not geo_sig or geo.covers_any(geo_sig, allowed)):
        return Verdict.MATCH

    # Step 2: unknown location
    if "unknown_location" in tags:
        return Verdict.UNKNOWN if loc.allow_unknown else Verdict.REJECT

    # Step 3: remote posting — match if the posting's eligible area covers any
    # allowed country ("Remote - Europe" covers DE; "Worldwide" covers all).
    if "remote" in tags:
        if loc.remote_policy == "anywhere":
            return Verdict.MATCH
        if not geo_sig:
            return Verdict.UNKNOWN  # remote but no geo signal
        return Verdict.MATCH if geo.covers_any(geo_sig, allowed) else Verdict.REJECT

    # Step 4: onsite/hybrid in some non-allowed location
    return Verdict.REJECT


def filter_stack(p: NormalizedPosting, f: FiltersConfig) -> Verdict:
    if not p.stack:
        return Verdict.UNKNOWN
    wanted = {s.lower() for s in f.stack_any_of}
    return Verdict.MATCH if (p.stack & wanted) else Verdict.REJECT


def filter_comp(p: NormalizedPosting, f: FiltersConfig) -> Verdict:
    if p.comp_min is None:
        return Verdict.UNKNOWN
    return Verdict.MATCH if p.comp_min >= f.comp_floor_usd else Verdict.REJECT


def filter_age(p: NormalizedPosting, f: FiltersConfig) -> Verdict:
    if f.max_age_days is None:
        return Verdict.MATCH
    if p.posted_at is None:
        return Verdict.UNKNOWN
    posted = p.posted_at if p.posted_at.tzinfo else p.posted_at.replace(tzinfo=timezone.utc)
    age_days = (datetime.now(tz=timezone.utc) - posted).days
    return Verdict.MATCH if age_days <= f.max_age_days else Verdict.REJECT


_PIPELINE = [
    ("age", filter_age),
    ("company", filter_company),
    ("role", filter_role),
    ("employment_type", filter_employment_type),
    ("seniority", filter_seniority),
    ("location", filter_location),
    ("stack", filter_stack),
    ("comp", filter_comp),
]


def evaluate(p: NormalizedPosting, f: FiltersConfig) -> Decision:
    decision = Decision(allow=True)
    for name, fn in _PIPELINE:
        v = fn(p, f)
        if v is Verdict.REJECT:
            return Decision(allow=False, rejected_by=name)
        if v is Verdict.UNKNOWN:
            decision.unknowns.append(name)
    return decision
