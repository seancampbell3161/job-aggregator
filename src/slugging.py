"""Deterministic company-name → ATS-slug variant generation for the discovery
conversion chain (spec: 2026-07-10-candidate-conversion-chain-design.md).
Pure string logic — no HTTP, no store access, no project imports (this module
sits below src.yc_oss and src.discovery in the import graph)."""

from __future__ import annotations

import re
from urllib.parse import urlparse

# Characters no ATS board slug accepts. Conservative, spec-locked class.
_PUNCT_RE = re.compile(r"""[.,'"()!&/+]""")
_HYPHEN_RUN_RE = re.compile(r"-{2,}")
# Trailing corporate suffixes, stripped at most once. Conservative by design.
_CORP_SUFFIXES = ("-inc", "-llc", "-ltd", "-corp")
# Common second-level registry labels: the company label sits LEFT of these
# (airbyte.co.uk → airbyte), not at the usual second-from-right position.
_COMMON_SLDS = frozenset({"co", "com", "net", "org", "ac", "gov", "edu"})


def normalize_name_slug(name: str) -> str:
    """Variant 1: the historical derive_slug rule plus punctuation strip,
    hyphen-run collapse, and trailing corporate-suffix strip. Returns '' for
    empty or all-punctuation names."""
    s = _PUNCT_RE.sub("", name.lower().strip())
    s = s.replace(" ", "-")
    s = _HYPHEN_RUN_RE.sub("-", s).strip("-")
    for suffix in _CORP_SUFFIXES:
        if s.endswith(suffix):
            s = s[: -len(suffix)].rstrip("-")
            break
    return s


def _hostname(website: str) -> str | None:
    """Lowercased hostname of a (possibly schemeless) URL; None when the
    input has no dot-separated host at all."""
    try:
        parsed = urlparse(website if "://" in website else f"https://{website}")
    except ValueError:
        return None
    host = (parsed.hostname or "").lower().strip(".")
    return host if host and "." in host else None


def seed_domain(website: str) -> str | None:
    """Bare probe domain for fingerprint_company — scheme, path, and a
    leading www. stripped: https://www.airbyte.com/x → airbyte.com."""
    host = _hostname(website)
    if host is None:
        return None
    return host[4:] if host.startswith("www.") else host


def domain_slug(website: str) -> str | None:
    """Variant 2: first label of the registrable domain.
    https://www.airbyte.com/x → airbyte; airbyte.co.uk → airbyte."""
    host = _hostname(website)
    if host is None:
        return None
    labels = host.split(".")
    if len(labels) >= 3 and labels[-2] in _COMMON_SLDS:
        return labels[-3]
    return labels[-2]


def labeled_slug_candidates(
    name: str, website: str | None, alt_slug: str | None
) -> list[tuple[str, str]]:
    """Ordered, deduped (kind, slug) pairs — kind ∈ {"slug", "domain", "alt"} —
    used both to probe and to build methods_tried labels. ≤3 by construction
    (one per kind); empty-safe on every input."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    v1 = normalize_name_slug(name)
    if v1:
        out.append(("slug", v1))
        seen.add(v1)
    v2 = domain_slug(website) if website else None
    if v2 and v2 not in seen:
        out.append(("domain", v2))
        seen.add(v2)
    v3 = (alt_slug or "").strip().lower()
    if v3 and v3 not in seen:
        out.append(("alt", v3))
    return out


def slug_candidates(name: str, website: str | None, alt_slug: str | None) -> list[str]:
    """Spec-public form: the ordered variant slugs without kind labels."""
    return [slug for _, slug in labeled_slug_candidates(name, website, alt_slug)]
