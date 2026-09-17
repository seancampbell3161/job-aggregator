"""Would importing a settings file undo settings saved since the last import?

add-source, the --merge scripts, seed_companies, and restore change settings in
the database only, so a user's files can fall behind. Three canonical settings
documents tell a stale file from a deliberate edit: the last import (base), the
settings in effect (current), and the incoming file. A setting that changed
since the import but still carries its old value in the file is stale; one that
carries a new value was edited on purpose."""
from __future__ import annotations

import json

# Canonical documents omit settings at their defaults, so a missing key means
# "the default".
_MISSING = object()
_MAX_LISTED_ITEMS = 5


def undone_changes(base: dict | None, current: dict, incoming: dict) -> list[str]:
    """One line per setting the incoming document would undo, as
    "dotted.path: what would happen", in document order.

    With a ``base`` (the last import's document), only settings that changed
    since that import count: a value counts when the file still carries the
    value from the import; a list counts entries added since the import that
    the file lacks and entries removed since the import that the file still has
    (order is ignored). Without a base (the settings never came from an import)
    every value in effect counts, and the file must keep each one."""
    lines: list[str] = []
    _compare(base, current, incoming, "", lines)
    return lines


def _compare(base: dict | None, current: dict, incoming: dict, prefix: str, out: list[str]) -> None:
    keys = dict.fromkeys([*current, *(base or {})])
    for key in keys:
        path = f"{prefix}{key}"
        old = _MISSING if base is None else base.get(key, _MISSING)
        now = current.get(key, _MISSING)
        new = incoming.get(key, _MISSING)
        if base is not None and old == now:
            continue  # unchanged since the import: the file may set anything
        if isinstance(now, dict) or isinstance(old, dict):
            _compare(None if base is None else _as_dict(old), _as_dict(now), _as_dict(new),
                     f"{path}.", out)
        elif isinstance(now, list) or isinstance(old, list):
            _compare_lists(path, None if base is None else _as_list(old), _as_list(now),
                           _as_list(new), out)
        elif base is not None:
            if new == old:
                out.append(f"{path}: would change {_show(now)} back to {_show(old)}")
        elif new != now:
            out.append(f"{path}: would change {_show(now)} to {_show(new)}")


def _compare_lists(path: str, base: list | None, current: list, incoming: list, out: list[str]) -> None:
    if base is None:
        dropped = [item for item in current if item not in incoming]
        restored: list = []
    else:
        dropped = [item for item in current if item not in base and item not in incoming]
        restored = [item for item in base if item not in current and item in incoming]
    if dropped:
        out.append(f"{path}: would drop {_items(dropped)}")
    if restored:
        out.append(f"{path}: would bring back {_items(restored)}")


def _as_dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _as_list(value: object) -> list:
    return value if isinstance(value, list) else []


def _show(value: object) -> str:
    return "the default" if value is _MISSING else _item(value)


def _item(value: object) -> str:
    return value if isinstance(value, str) else json.dumps(value, sort_keys=True)


def _items(values: list) -> str:
    shown = ", ".join(_item(v) for v in values[:_MAX_LISTED_ITEMS])
    extra = len(values) - _MAX_LISTED_ITEMS
    return f"{shown} and {extra} more" if extra > 0 else shown
