"""The apply-kit facts scaffold: contact block + settings -> facts.yaml text.

No LLM call, by design. facts.yaml is three different kinds of thing. Links are
in a résumé. Location and desired salary are already in the settings document,
because the wizard's interview binds those answers to real config paths. Work
authorization, sponsorship and every EEO field are in neither, and they go out
on real applications — so they are emitted blank, from a literal list, with no
code path by which model output could reach them."""
from __future__ import annotations

from typing import Any, Mapping

import yaml

from src.config import AppConfig

_EEO_LABELS = ("Gender", "Race/ethnicity", "Veteran status", "Disability")


def _text(value: Any) -> str:
    return str(value or "").strip()


def build_facts_yaml(contact: Mapping[str, Any], cfg: AppConfig) -> str:
    location = cfg.filters.location
    where = ", ".join(location.allowed_cities) or ", ".join(location.allowed_countries)
    comp = cfg.filters.comp_floor_usd

    groups = [
        ("Links", [
            ("GitHub", _text(contact.get("github"))),
            ("LinkedIn", _text(contact.get("linkedin"))),
            ("Personal site", _text(contact.get("website"))),
            ("Email", _text(contact.get("email"))),
            ("Phone", _text(contact.get("phone"))),
        ]),
        ("Eligibility", [
            ("Location", where),
            ("Desired salary (USD)", str(comp) if comp else ""),
            ("Work authorization", ""),
            ("Requires sponsorship", ""),
            ("Willing to relocate", ""),
            ("Earliest start", ""),
        ]),
        # A literal list of labels with literal empty values. Nothing derived,
        # nothing from `contact`.
        ("EEO", [(label, "") for label in _EEO_LABELS]),
    ]

    payload = [
        {"group": name,
         "facts": [{"label": label, "value": value} for label, value in facts]}
        for name, facts in groups
    ]
    # safe_dump quotes anything YAML would otherwise reinterpret, which is what
    # keeps a bare "No" a string rather than False.
    return yaml.safe_dump(payload, sort_keys=False, allow_unicode=True,
                          default_flow_style=False)
