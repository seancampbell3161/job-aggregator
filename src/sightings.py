"""Hiring.cafe sighting capture: classify foreign postings' apply URLs into
slug-family candidates (discovered_slugs) or structured board candidates
(discovered_boards). Capture is pure string parsing — zero HTTP; validation
happens later on the discovery tier's budget (src.discovery)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import urlparse

from src.fingerprint import (
    parse_ats_url,
    parse_eightfold_url,
    parse_jsonld_url,
    parse_taleo_url,
)

log = logging.getLogger(__name__)

# Families the slug probe validates (src.discovery._SUPPORTED_ATS).
PROBEABLE_FAMILIES = frozenset(
    {"greenhouse", "lever", "ashby", "workable", "smartrecruiters", "rippling",
     "personio", "recruitee", "teamtailor"}
)


@dataclass(frozen=True)
class Sighting:
    """One foreign posting seen by an aggregator connector (hiring.cafe, adzuna)."""
    ats_family: str | None  # the aggregator's claimed ATS label, lowercased
    slug: str | None        # the aggregator's claimed board token
    company: str
    apply_url: str
    origin: str = "hiringcafe"  # which connector saw it — recorded on candidate rows


def classify_sighting(s: Sighting) -> tuple[str, dict] | None:
    """(family, identity) for a sighting, else None. A classified apply_url
    (verifiable structure) wins over the claimed label; the claim is trusted
    only as a fallback and only for probe-validatable slug families."""
    for parser in (parse_ats_url, parse_eightfold_url, parse_jsonld_url, parse_taleo_url):
        try:
            parsed = parser(s.apply_url)
        except Exception:  # noqa: BLE001 — a malformed URL must not kill the batch
            parsed = None
        if parsed:
            return parsed
    if s.ats_family in PROBEABLE_FAMILIES and s.slug:
        return s.ats_family, {"slug": s.slug}
    return None


def drain_sightings(
    sightings: list[Sighting],
    *,
    discovered,
    boards,
    cap: int,
    no_match_fresh_days: int,
) -> dict[str, int]:
    """Classify + stage a cycle's sightings as candidate rows. Per-sighting
    fail-soft; stops after ``cap`` new rows. Returns counters for the log."""
    counts = {
        "seen": len(sightings), "captured_slugs": 0, "captured_boards": 0,
        "deduped": 0, "unclassified": 0, "no_board_store": 0,
    }
    captured = 0
    for s in sightings:
        if captured >= cap:
            break
        try:
            result = classify_sighting(s)
            if result is None:
                counts["unclassified"] += 1
                continue
            family, identity = result
            if family in PROBEABLE_FAMILIES:
                slug = identity["slug"]
                key = f"{family}:{slug}"
                if (
                    discovered.get(key) is not None
                    or discovered.get(f"candidate:{slug}") is not None
                    or discovered.is_recent_no_match(slug, fresh_within_days=no_match_fresh_days)
                ):
                    counts["deduped"] += 1
                    continue
                discovered.upsert_candidate(
                    key, company_name=s.company or None,
                    origin=s.origin, claimed_family=family,
                )
                counts["captured_slugs"] += 1
                captured += 1
            else:
                if boards is None:
                    counts["no_board_store"] += 1
                    continue
                host = (urlparse(s.apply_url).hostname or "").lower()
                if not host:
                    counts["unclassified"] += 1
                    continue
                if boards.get(host) is not None:
                    counts["deduped"] += 1
                    continue
                conn_name = None
                try:
                    from src.connectors.base import connector_from_identity
                    conn = connector_from_identity(family, dict(identity), s.company or None)
                    conn_name = conn.name if conn is not None else None
                except Exception:  # noqa: BLE001 — partial identity (eightfold): resolved at validation
                    conn_name = None
                boards.upsert_candidate(
                    host, name=s.company or host, family=family, identity=identity,
                    connector_name=conn_name, company=s.company or None,
                    origin=s.origin,
                )
                counts["captured_boards"] += 1
                captured += 1
        except Exception:  # noqa: BLE001 — one bad sighting/store hiccup must not kill the drain
            log.exception("sighting_capture_failed", extra={"apply_url": s.apply_url})
            continue
    log.info("hiringcafe_sightings_captured", extra=counts)
    return counts
