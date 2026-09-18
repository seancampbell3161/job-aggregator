"""diff_settings: a plain, symmetric "what changed" between two complete
settings documents.

This is a different question from src.settings.import_guard.undone_changes,
which is a three-way, directional question about whether importing a file
would undo a change saved since the last import. Neither function is
expressible in terms of the other; they share only how a value, a list entry,
and an unset optional section are rendered — the helpers below."""
from __future__ import annotations

import json

from src.config import AppConfig

MAX_LISTED_ITEMS = 5


def diff_settings(old: AppConfig, new: AppConfig) -> list[str]:
    """One line per difference, as "dotted.path: what changed", in settings
    order. Lists compare as sets of whole entries, because a settings list is
    a collection of boards or queries and its order carries no meaning: a
    structured entry (e.g. a Workday tenant) is reported as one whole value,
    never field by field."""
    lines: list[str] = []
    _walk(_values(old), _values(new), "", lines)
    return lines


def _values(cfg: AppConfig) -> dict:
    return cfg.model_dump(mode="json", exclude={"secrets"})


def _walk(old: dict, new: dict, prefix: str, out: list[str]) -> None:
    for key in dict.fromkeys([*old, *new]):
        path = f"{prefix}{key}"
        was = old.get(key)
        now = new.get(key)
        if was == now:
            continue
        if isinstance(was, dict) and isinstance(now, dict):
            _walk(was, now, f"{path}.", out)
        elif isinstance(was, list) and isinstance(now, list):
            _walk_list(path, was, now, out)
        else:
            out.append(f"{path}: {show_item(was)} -> {show_item(now)}")


def _walk_list(path: str, old: list, new: list, out: list[str]) -> None:
    removed = [item for item in old if item not in new]
    added = [item for item in new if item not in old]
    if removed:
        out.append(f"{path}: - {show_items(removed)}")
    if added:
        out.append(f"{path}: + {show_items(added)}")


def show_item(value: object) -> str:
    """Plain strings as-is; empty, padded, or comma-holding strings and every
    other value as JSON, so each item reads unambiguously in a list."""
    if isinstance(value, str) and value and value == value.strip() and "," not in value:
        return value
    return json.dumps(value, sort_keys=True)


def show_items(values: list) -> str:
    shown = ", ".join(show_item(v) for v in values[:MAX_LISTED_ITEMS])
    extra = len(values) - MAX_LISTED_ITEMS
    return f"{shown} and {extra} more" if extra > 0 else shown
