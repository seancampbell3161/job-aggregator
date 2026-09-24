"""Display text for stored choice values (config Literals, interview choices).
The stored value never changes — this is only what a person reads."""
from __future__ import annotations

# Values whose mechanical form would read wrong.
_OVERRIDES = {
    "ic": "Individual contributor",
    "anthropic": "Anthropic (Claude)",
    "gemini": "Google (Gemini)",
    "allowed_countries": "Only in my countries",
}


def choice_label(value: str) -> str:
    value = str(value)
    if value in _OVERRIDES:
        return _OVERRIDES[value]
    text = value.replace("_", " ")
    return text[:1].upper() + text[1:]
