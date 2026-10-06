"""Choose which live boards go in the pack (spec §4, "corrected B").

Per region: walk boards by regional posting count (desc), adding each one that
fits both the board limit and the postings-per-cycle budget. A board that
would bust the budget is skipped, not a stop, so smaller boards keep filling
what remains. The limits are what today's poll cycle handles. Step 3 of the
max-coverage effort raises them."""
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
    def regional(b: LiveBoard) -> int:
        return b.result.us_postings if region == "us" else b.result.eu_postings

    ranked = sorted(boards, key=lambda b: (-regional(b), -b.result.postings, b.key))
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


def select(live: Iterable[LiveBoard], *, us: Limits = US_LIMITS,
           eu: Limits = EU_LIMITS) -> dict[str, RegionPick]:
    by_region: dict[str, list[LiveBoard]] = {"us": [], "eu": []}
    for b in live:
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
