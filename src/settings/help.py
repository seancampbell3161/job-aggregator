"""Per-flag help for the settings UI, parsed from docs/CONFIG.md.

tests/test_config_docs.py fails CI whenever a flag lacks a row there, so the
reference doubles as the UI's help source and cannot drift from the model.
A missing or unreadable file is soft — the UI simply shows no help."""
from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

log = logging.getLogger(__name__)

_DOC_PATH = Path(__file__).resolve().parents[2] / "docs" / "CONFIG.md"

_PATH_RE = re.compile(r"[a-z_][a-z_0-9]*(?:\.[a-z_0-9]+)+")
_HEADING_RE = re.compile(r"^##\s+(\S+)\s*$")
_CODE_RE = re.compile(r"`([^`]+)`")
_BOLD_RE = re.compile(r"\*\*([^*]+)\*\*")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")

# Abbreviations whose period does not end a sentence.
_ABBREV = ("e.g.", "i.e.", "etc.", "vs.", "cf.")


@dataclass(frozen=True)
class Help:
    summary: str  # first sentence, rendered to HTML
    full: str     # the whole cell, rendered to HTML


def render_inline(markdown: str) -> str:
    """Escape, then re-introduce the inline markup CONFIG.md actually uses.
    Deliberately not a markdown dependency — three constructs, one pass each."""
    out = html.escape(markdown, quote=False)
    out = _LINK_RE.sub(lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>', out)
    out = _BOLD_RE.sub(r"<strong>\1</strong>", out)
    out = _CODE_RE.sub(r"<code>\1</code>", out)
    return out


def _first_sentence(text: str) -> str:
    for m in re.finditer(r"[.!?](?=\s|$)", text):
        head = text[: m.end()]
        if any(head.endswith(a) for a in _ABBREV):
            continue
        return head
    return text


def _row(line: str) -> tuple[str, str] | None:
    """(dotted path, description) for a flag row; None for anything else.

    Splits on every '|' and rejoins the middle so a description containing a
    pipe survives. Column 2 is the default for settings flags and the env var
    for secrets — neither is used: defaults come from the model."""
    if not line.startswith("|"):
        return None
    parts = line.rstrip().split("|")
    if len(parts) < 5:
        return None
    path = parts[1].strip().strip("`")
    if not _PATH_RE.fullmatch(path):
        return None  # header row, separator row, or a non-flag table
    return path, "|".join(parts[3:-1]).strip()


def parse(text: str) -> tuple[dict[str, Help], dict[str, str]]:
    """(help by dotted path, intro prose by top-level section)."""
    helps: dict[str, Help] = {}
    intros: dict[str, str] = {}
    section: str | None = None
    intro_lines: list[str] = []

    def flush() -> None:
        if section is not None:
            intros[section] = render_inline(" ".join(intro_lines).strip())

    for line in text.splitlines():
        heading = _HEADING_RE.match(line)
        if heading:
            flush()
            section, intro_lines = heading.group(1), []
            continue
        parsed = _row(line)
        if parsed is not None:
            path, description = parsed
            helps[path] = Help(
                summary=render_inline(_first_sentence(description)),
                full=render_inline(description),
            )
            continue
        if section is not None and not helps.get(f"{section}.") and line.strip() and not line.startswith("|"):
            intro_lines.append(line.strip())
    flush()
    return helps, intros


@lru_cache(maxsize=1)
def _load() -> tuple[dict[str, Help], dict[str, str]]:
    try:
        return parse(_DOC_PATH.read_text(encoding="utf-8"))
    except OSError as exc:
        log.warning("config_help_unavailable", extra={"path": str(_DOC_PATH), "error": str(exc)})
        return {}, {}


def field_help(path: str) -> Help | None:
    return _load()[0].get(path)


def group_intro(key: str) -> str:
    return _load()[1].get(key, "")
