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

from src.config import AppConfig
from src.settings.diff import show_item, show_items

# A default the comparison can't know: fields inside an optional section, whose
# own default is None.
_UNKNOWN = object()


def undone_changes(base: AppConfig | None, current: AppConfig, incoming: AppConfig) -> list[str]:
    """One line per setting the incoming settings would undo, as
    "dotted.path: what would happen", in settings order.

    With a ``base`` (the last import's settings), only settings that changed
    since that import count: a value counts when the file still carries the
    value from the import; a list counts entries added since the import that
    the file lacks and entries removed since the import that the file still has
    (order is ignored), comparing entries as whole values; an optional section
    unset since the import counts when the file sets it at all. Without a base
    (the settings never came from an import) every setting that differs from
    its default counts, and the file must keep it: a value exactly; a list every
    entry, and none of the default entries it no longer has."""
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
    # Model sections share their keys; a free-form mapping's may differ, and a
    # missing key reads as unset.
    for key in dict.fromkeys([*current, *(base or {})]):
        path = f"{prefix}{key}"
        now = current.get(key)
        new = incoming.get(key)
        default = _UNKNOWN if defaults is None else defaults.get(key, _UNKNOWN)
        old = _UNKNOWN if base is None else base.get(key)
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
        elif new == old or _all_are(dict, old, new):
            # A section unset since the import stays unset, like a removed list
            # entry — even when the file edits it.
            out.append(_change(path, now, new, default, "back to"))


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
        out.append(f"{path}: would drop {show_items(dropped)}")
    if restored:
        out.append(f"{path}: would bring back {show_items(restored)}")


def _all_are(kind: type, *values: object) -> bool:
    return all(isinstance(v, kind) for v in values if v is not _UNKNOWN)


def _change(path: str, now: object, target: object, default: object, direction: str) -> str:
    if target is None:
        return f"{path}: would unset it (now {_show(now, default)})"
    if now is None:
        return f"{path}: would set it {direction} {_show(target, default)}"
    return f"{path}: would change {_show(now, default)} {direction} {_show(target, default)}"


def _show(value: object, default: object) -> str:
    return f"{show_item(value)} (the default)" if value == default else show_item(value)
