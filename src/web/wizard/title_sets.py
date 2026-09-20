"""Common job-title variants, offered as one-click sets.

`filters.titles` is an exact-phrase allowlist: nothing matches a title the user
did not list. One role is advertised under many names — software engineer,
software developer, programmer — so a user who lists only the wording their own
employer used quietly filters out most of the market, and nothing on the page
tells them that happened.

A set is a starting point, not a taxonomy. Everything it adds is an ordinary
chip the user can edit or delete, and the sets are deliberately shallow: enough
to break the "I typed one title and got nothing" trap, not an attempt to
enumerate every job in the world.

Values are lowercase because matching is case-insensitive on word boundaries
(docs/CONFIG.md, filters.titles) — storing them lowercase keeps what the user
sees in the box identical to what is compared.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TitleSet:
    key: str
    label: str
    titles: tuple[str, ...]


TITLE_SETS: tuple[TitleSet, ...] = (
    TitleSet("software", "Software engineer", (
        "software engineer", "software developer", "programmer",
    )),
    TitleSet("backend", "Backend", (
        "backend engineer", "backend developer", "api engineer",
        "server engineer",
    )),
    TitleSet("frontend", "Frontend", (
        "frontend engineer", "frontend developer", "ui engineer",
        "web developer",
    )),
    TitleSet("fullstack", "Full stack", (
        "full stack engineer", "full stack developer", "fullstack engineer",
    )),
    TitleSet("platform", "Platform / infra", (
        "platform engineer", "infrastructure engineer", "devops engineer",
        "site reliability engineer",
    )),
    TitleSet("data", "Data", (
        "data engineer", "analytics engineer", "data platform engineer",
    )),
    TitleSet("mobile", "Mobile", (
        "mobile engineer", "mobile developer", "ios engineer",
        "android engineer",
    )),
    TitleSet("management", "Engineering management", (
        "engineering manager", "software engineering manager",
        "development manager",
    )),
)
