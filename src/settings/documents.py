"""Document kinds stored beside the settings document: save-time validation
and the read-side Documents bundle a ConfigSnapshot carries."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from src.settings.errors import SettingsInvalid

if TYPE_CHECKING:
    from src.tailor.models import EvidenceBank, ResumeContent

log = logging.getLogger(__name__)

DOCUMENT_KINDS: tuple[str, ...] = ("profile", "resume_text", "resume_content", "evidence", "kit_facts")

_TEXT_KINDS = ("profile", "resume_text")


def _parse_content(body: str):
    from src.tailor.content import parse_content
    return parse_content(body)


def _parse_evidence(body: str):
    from src.tailor.evidence import parse_evidence
    return parse_evidence(body)


def _parse_facts(body: str):
    from src.kit_facts import parse_facts
    return parse_facts(body)


_PARSERS: dict[str, Callable[[str], object]] = {
    "resume_content": _parse_content,
    "evidence": _parse_evidence,
    "kit_facts": _parse_facts,
}

# What the parsers raise on malformed input (JSONDecodeError is a ValueError;
# FactsError is imported lazily below).
_PARSE_ERRORS: tuple[type[Exception], ...] = (ValueError, TypeError, KeyError, AttributeError)


def _parse_errors() -> tuple[type[Exception], ...]:
    from src.kit_facts import FactsError
    return (*_PARSE_ERRORS, FactsError)


def validate_document(kind: str, body: str) -> None:
    """Raise SettingsInvalid (loc = kind) unless ``body`` is a usable document
    of ``kind``. profile/resume_text must be non-blank; the structured kinds
    must parse with the same parsers their consumers use."""
    if kind not in DOCUMENT_KINDS:
        raise SettingsInvalid([{"loc": "kind", "msg": f"unknown document kind {kind!r}"}])
    if kind in _TEXT_KINDS:
        if not body.strip():
            raise SettingsInvalid([{"loc": kind, "msg": "must not be empty"}])
        return
    try:
        _PARSERS[kind](body)
    except _parse_errors() as exc:
        raise SettingsInvalid([{"loc": kind, "msg": str(exc) or type(exc).__name__}]) from exc


def _parse_or_none(kind: str, body: str | None):
    if body is None:
        return None
    try:
        return _PARSERS[kind](body)
    except _parse_errors() as exc:
        log.warning("document_unparseable", extra={"kind": kind, "error": str(exc)})
        return None


@dataclass(frozen=True)
class Documents:
    """Raw text of the newest document of each kind (None = never saved)."""

    profile: str | None = None
    resume_text: str | None = None
    resume_content: str | None = None
    evidence: str | None = None
    kit_facts: str | None = None

    def content(self) -> "ResumeContent | None":
        """Parsed résumé bank; None when absent or unparseable (logged)."""
        return _parse_or_none("resume_content", self.resume_content)

    def evidence_bank(self) -> "EvidenceBank | None":
        """Parsed evidence bank; None when absent or unparseable (logged)."""
        return _parse_or_none("evidence", self.evidence)
