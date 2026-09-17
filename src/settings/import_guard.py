"""Would importing a settings file undo settings saved since the last import?

add-source, the --merge scripts, seed_companies, and restore change settings in
the database only, so a user's files can fall behind. Three sets of settings
tell a stale file from a deliberate edit: the last import (base), the settings
in effect (current), and the incoming file. A setting that changed since the
import but still carries its old value in the file is stale; one that carries a
new value was edited on purpose.

The comparison runs on complete settings — every field, defaults included — so
a setting a file leaves out counts as its default, just as it will once imported."""
from __future__ import annotations

import json

from src.config import AppConfig

# A default the comparison can't know: fields inside an optional section, whose
# own default is None.
_UNKNOWN = object()
_MAX_LISTED_ITEMS = 5


def undone_changes(base: AppConfig | None, current: AppConfig, incoming: AppConfig) -> list[str]:
    """One line per setting the incoming settings would undo, as
    "dotted.path: what would happen", in settings order.

    With a ``base`` (the last import's settings), only settings that changed
    since that import count: a value counts when the file still carries the
    value from the import; a list counts entries added since the import that
    the file lacks and entries removed since the import that the file still has
    (order is ignored). Without a base (the settings never came from an import)
    every setting that differs from its default counts, and the file must keep
    it: a value exactly; a list every entry, and none of the default entries it
    no longer has."""
    lines: list[str] = []
    _compare(None if base is None else _values(base), _values(current), _values(incoming),
             _values(AppConfig()), "", lines)
    return lines


def _values(cfg: AppConfig) -> dict:
    return cfg.model_dump(mode="json", exclude={"secrets"})


def _compare(
    base: dict | None, current: dict, incoming: dict, defaults: dict | None,
    prefix: str, out: list[str],
) -> None:
    for key, now in current.items():
        path = f"{prefix}{key}"
        new = incoming[key]
        default = _UNKNOWN if defaults is None else defaults[key]
        old = _UNKNOWN if base is None else base[key]
        if now == (default if base is None else old):
            continue  # unchanged: the file may set anything
        if _all_are(dict, now, new, old):
            _compare(None if base is None else old, now, new,
                     default if isinstance(default, dict) else None, f"{path}.", out)
        elif _all_are(list, now, new, old):
            _compare_lists(path, None if base is None else old, now, new,
                           default if isinstance(default, list) else [], out)
        elif base is None:
            if new != now:
                out.append(_change(path, now, new, default, "to"))
        elif new == old:
            out.append(_change(path, now, old, default, "back to"))


def _compare_lists(
    path: str, base: list | None, current: list, incoming: list, default: list, out: list[str],
) -> None:
    if base is None:
        dropped = [item for item in current if item not in incoming]
        restored = [item for item in default if item not in current and item in incoming]
    else:
        dropped = [item for item in current if item not in base and item not in incoming]
        restored = [item for item in base if item not in current and item in incoming]
    if dropped:
        out.append(f"{path}: would drop {_items(dropped)}")
    if restored:
        out.append(f"{path}: would bring back {_items(restored)}")


def _all_are(kind: type, *values: object) -> bool:
    return all(isinstance(v, kind) for v in values if v is not _UNKNOWN)


def _change(path: str, now: object, target: object, default: object, direction: str) -> str:
    if target is None:
        return f"{path}: would unset it (now {_show(now, default)})"
    if now is None:
        return f"{path}: would set it {direction} {_show(target, default)}"
    return f"{path}: would change {_show(now, default)} {direction} {_show(target, default)}"


def _show(value: object, default: object) -> str:
    return f"{_item(value)} (the default)" if value == default else _item(value)


def _item(value: object) -> str:
    """Plain strings as-is; empty, padded, or comma-holding strings and every
    other value as JSON, so each item reads unambiguously in a list."""
    if isinstance(value, str) and value and value == value.strip() and "," not in value:
        return value
    return json.dumps(value, sort_keys=True)


def _items(values: list) -> str:
    shown = ", ".join(_item(v) for v in values[:_MAX_LISTED_ITEMS])
    extra = len(values) - _MAX_LISTED_ITEMS
    return f"{shown} and {extra} more" if extra > 0 else shown
