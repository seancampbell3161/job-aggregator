"""Choose which live boards go in the pack (spec §4, "regional share").

Per region: walk boards by regional share (desc), then smaller boards first
(asc), adding each one that fits both the board limit and the postings-per-cycle
budget. A board that would bust the budget is skipped, not a stop, so smaller
boards keep filling what remains. Regional share is the fraction of postings
serving that region; polling spent on non-regional postings is waste for that
region's users. Smaller boards first fits the most companies under today's
polling capacity; step 3 of the max-coverage effort raises the limits."""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from scripts.pack_builder.cache import CheckResult
from src.starter_pack import SLUG_FAMILIES


@dataclass(frozen=True)
class Limits:
    max_boards: int
    max_postings: int


US_LIMITS = Limits(max_boards=4_000, max_postings=100_000)
EU_LIMITS = Limits(max_boards=1_000, max_postings=20_000)


@dataclass(frozen=True)
class LiveBoard:
    key: str
    family: str
    result: CheckResult


@dataclass
class RegionPick:
    boards: list[LiveBoard]
    postings: int
    binding: str  # "boards" | "postings" | "none"


def region_of(result: CheckResult) -> str | None:
    if result.us_postings > 0:
        return "us"
    if result.eu_postings > 0:
        return "eu"
    return None


def fill(boards: Iterable[LiveBoard], limits: Limits, region: str) -> RegionPick:
    """Fill the regional quota by share, then smaller boards first.

    Regional share is (region_postings / total_postings); for postings == 0,
    treat share as 0 (though live boards always have >= 1).
    """
    def regional(b: LiveBoard) -> int:
        return b.result.us_postings if region == "us" else b.result.eu_postings

    def share(b: LiveBoard) -> float:
        r = regional(b)
        return r / b.result.postings if b.result.postings > 0 else 0

    ranked = sorted(boards, key=lambda b: (-share(b), b.result.postings, b.key))
    picked: list[LiveBoard] = []
    total = 0
    skipped = False
    for b in ranked:
        if len(picked) >= limits.max_boards:
            break
        if total + b.result.postings > limits.max_postings:
            skipped = True
            continue
        picked.append(b)
        total += b.result.postings
    binding = "boards" if len(picked) >= limits.max_boards else ("postings" if skipped else "none")
    return RegionPick(picked, total, binding)


def _regional(result: CheckResult) -> int:
    region = region_of(result)
    if region == "us":
        return result.us_postings
    return result.eu_postings if region == "eu" else 0


def dedup_by_connector(live: Iterable[LiveBoard]) -> list[LiveBoard]:
    """One board per connector_name. Distinct candidates can share one: a
    Workday tenant:site crawled under two data-centre regions (wd1 and wd503),
    or two portals mapping to the same icims:{slug}. Packed together they would
    share domain pack:{connector_name}, poll the same board twice and
    double-count the budgets. Keep the one with the most regional postings,
    then the most postings, then the lowest key."""
    best: dict[str, LiveBoard] = {}
    for b in live:
        name = b.result.connector_name or b.key
        cur = best.get(name)
        if cur is None or _rank(b) < _rank(cur):
            best[name] = b
    return list(best.values())


def _rank(b: LiveBoard) -> tuple:
    return (-_regional(b.result), -b.result.postings, b.key)


def select(live: Iterable[LiveBoard], *, us: Limits = US_LIMITS,
           eu: Limits = EU_LIMITS) -> dict[str, RegionPick]:
    by_region: dict[str, list[LiveBoard]] = {"us": [], "eu": []}
    for b in dedup_by_connector(live):
        region = region_of(b.result)
        if region is not None:
            by_region[region].append(b)
    return {"us": fill(by_region["us"], us, "us"), "eu": fill(by_region["eu"], eu, "eu")}


def to_pack(picks: dict[str, RegionPick], version: str) -> dict:
    slugs: list[dict] = []
    boards: list[dict] = []
    for region, pick in picks.items():
        for b in pick.boards:
            r = b.result
            if b.family in SLUG_FAMILIES:
                slugs.append({"ats": b.family, "slug": r.identity["slug"], "company": r.company,
                              "region": region, "postings": r.postings})
            else:
                boards.append({"family": b.family, "identity": r.identity, "company": r.company,
                               "domain": f"pack:{r.connector_name}",
                               "connector_name": r.connector_name,
                               "region": region, "postings": r.postings})
    return {"version": version, "slugs": slugs, "boards": boards}
