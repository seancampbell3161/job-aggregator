"""Settings mutations for slug-list source families (greenhouse, lever, …).
Shared by `python -m src.settings add-source` and scripts/seed_companies.py."""
from __future__ import annotations

from typing import Mapping, Sequence

from src.config import SLUG_SOURCE_FAMILIES
from src.settings.service import ConfigService


def append_slug_sources(
    service: ConfigService, slugs_by_family: Mapping[str, Sequence[str]], *, label: str,
) -> dict[str, list[str]]:
    """Append slugs to their sources lists as one new settings version
    (source=cli), skipping slugs already present. Returns the slugs actually
    added per family ({} when nothing changed). Raises ValueError for a
    non-slug family, NotConfigured before setup."""
    for family in slugs_by_family:
        if family not in SLUG_SOURCE_FAMILIES:
            raise ValueError(
                f"unknown slug source family {family!r}; expected one of: "
                + ", ".join(SLUG_SOURCE_FAMILIES)
            )
    added: dict[str, list[str]] = {}

    def mutate(doc: dict) -> str | None:
        added.clear()  # mutate may run twice (StaleWrite retry)
        sources = doc.setdefault("sources", {})
        for family, slugs in slugs_by_family.items():
            current = sources.setdefault(family, [])
            for slug in slugs:
                if slug not in current:
                    current.append(slug)
                    added.setdefault(family, []).append(slug)
        if not added:
            return None
        names = [f"{family}:{slug}" for family, slugs in added.items() for slug in slugs]
        detail = ", ".join(names) if len(names) <= 5 else f"{len(names)} slugs"
        return f"{label}: added {detail}"

    service.update_settings(mutate, source="cli")
    return dict(added)
