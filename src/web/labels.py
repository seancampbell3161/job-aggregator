"""Display text for stored choice values (config Literals, interview choices).
The stored value never changes — this is only what a person reads."""
from __future__ import annotations

# Values whose mechanical form would read wrong.
_OVERRIDES = {"ic": "Individual contributor"}


def choice_label(value: str) -> str:
    value = str(value)
    if value in _OVERRIDES:
        return _OVERRIDES[value]
    text = value.replace("_", " ")
    return text[:1].upper() + text[1:]
