"""Dotted-path edits to a settings document.

Every writer of settings — the settings pages, the setup wizard, the résumé
draft — expresses its change as ``{"filters.titles": [...], ...}`` and applies
it here. It lives in the settings package rather than beside the web forms
because src/resume_intake/draft.py needs it too, and a domain module must not
depend on the web layer."""
from __future__ import annotations

from typing import Any, Mapping


def apply_patch(doc: dict, patch: Mapping[str, Any]) -> bool:
    """Set each dotted path in ``doc`` in place; a None value removes the key.

    Returns whether anything changed, so an untouched form writes no version."""
    changed = False
    for path, value in patch.items():
        parts = path.split(".")
        if value is None:
            changed |= _remove(doc, parts)
            continue
        node = doc
        for part in parts[:-1]:
            nxt = node.get(part)
            if not isinstance(nxt, dict):
                nxt = {}
                node[part] = nxt
                changed = True
            node = nxt
        if node.get(parts[-1]) != value or parts[-1] not in node:
            node[parts[-1]] = value
            changed = True
    return changed


def _remove(doc: dict, parts: list[str]) -> bool:
    node = doc
    trail: list[tuple[dict, str]] = []
    for part in parts[:-1]:
        nxt = node.get(part)
        if not isinstance(nxt, dict):
            return False
        trail.append((node, part))
        node = nxt
    if parts[-1] not in node:
        return False
    del node[parts[-1]]
    for parent, key in reversed(trail):  # prune parents left empty
        if parent[key] == {}:
            del parent[key]
    return True
