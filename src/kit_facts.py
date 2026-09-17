"""Apply-kit facts: free-form label/value groups, stored as the `kit_facts`
settings document (YAML text) and rendered on /kit with copy buttons."""
from __future__ import annotations

from dataclasses import dataclass

import yaml


class FactsError(Exception):
    """The facts YAML or its shape is wrong."""


@dataclass(frozen=True)
class Fact:
    label: str
    value: str


@dataclass(frozen=True)
class FactGroup:
    name: str
    facts: tuple[Fact, ...]


def _coerce(value: object) -> str:
    """Everything the template sees is a string. Unquoted YAML scalars are
    survived: booleans render Yes/No (a bare `No` parses as False), numbers
    via str(), null as empty string."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    return str(value)


def parse_facts(text: str) -> list[FactGroup]:
    """Parse facts YAML. Raises FactsError for YAML/shape problems. Ordering
    is preserved as written."""
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise FactsError(f"not valid YAML: {exc}") from exc
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise FactsError("top level must be a list of groups (each `- group: ...`)")
    groups: list[FactGroup] = []
    for i, entry in enumerate(raw, start=1):
        if not isinstance(entry, dict) or not entry.get("group"):
            raise FactsError(f"entry {i}: missing a `group:` name")
        name = str(entry["group"])
        facts_raw = entry.get("facts")
        if facts_raw is None:
            facts_raw = []
        if not isinstance(facts_raw, list):
            raise FactsError(f"group '{name}': `facts:` must be a list")
        facts: list[Fact] = []
        for j, f in enumerate(facts_raw, start=1):
            if not isinstance(f, dict) or not f.get("label"):
                raise FactsError(f"group '{name}' fact {j}: missing a `label:`")
            if "value" not in f:
                raise FactsError(f"group '{name}' fact {j}: missing a `value:`")
            facts.append(Fact(label=str(f["label"]), value=_coerce(f["value"])))
        groups.append(FactGroup(name=name, facts=tuple(facts)))
    return groups
