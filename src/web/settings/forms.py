"""Form body -> dotted-path patch, and validation errors -> per-field messages.

Every settings input is named by its dotted config path, which makes all three
directions (render, decode, place the error) dict lookups."""
from __future__ import annotations

from typing import Any, Callable, Iterable, Mapping, Sequence

from src.settings.errors import SettingsInvalid
from src.settings.fields import (
    KIND_BOOL, KIND_CHIPS, KIND_INT, KIND_MULTI_CHOICE, NON_FORM_KINDS, FieldSpec,
)

# An optional group (quiet_hours) renders an enable checkbox under this name.
GROUP_TOGGLE_SUFFIX = "__enabled"


def decode(
    fields: Sequence[FieldSpec],
    form: Mapping[str, list[str]],
    *,
    optional_groups: Iterable[str] = (),
) -> dict[str, Any]:
    """One patch entry per editable field the form could plausibly have sent.

    Iterating `fields` rather than the submitted keys is the whole point for a
    checkbox (bool or multi_choice): an unchecked box sends nothing, so a
    submitted-keys loop could never turn one off — those two kinds are always
    visited, "submitted or not". Every other kind's input always sends its
    key when the page it lives on is submitted (a blank text/number/chips
    input still posts an empty value), so a path that's genuinely absent from
    `form` there means this call wasn't given that field's page at all, not
    "the user cleared it" — skipping it leaves the effective value alone
    instead of forcing it back to the type default. This is what keeps a
    request scoped to one Advanced group from tripping an unrelated
    validator (e.g. sources.adzuna.countries' non-empty check) on a group
    with dozens of fields. Raises SettingsInvalid for values the form cannot
    represent."""
    groups = tuple(optional_groups)
    disabled = {g for g in groups if not form.get(f"{g}{GROUP_TOGGLE_SUFFIX}")}

    patch: dict[str, Any] = {g: None for g in disabled}
    errors: list[dict] = []

    for spec in fields:
        if not spec.editable or spec.kind in NON_FORM_KINDS:
            continue
        if any(spec.path == g or spec.path.startswith(g + ".") for g in disabled):
            continue  # the whole group is being removed
        if spec.kind not in (KIND_BOOL, KIND_MULTI_CHOICE) and spec.path not in form:
            continue  # not a checkbox, and never submitted at all: leave it
        values = form.get(spec.path, [])
        try:
            patch[spec.path] = _value(spec, values)
        except ValueError as exc:
            errors.append({"loc": spec.path, "msg": str(exc)})

    if errors:
        raise SettingsInvalid(errors)
    return patch


def _value(spec: FieldSpec, values: list[str]) -> Any:
    if spec.kind == KIND_BOOL:
        return bool(values)
    if spec.kind == KIND_MULTI_CHOICE:
        return [v for v in values if v in spec.choices]
    if spec.kind == KIND_CHIPS:
        return [v.strip() for v in values if v.strip()]

    raw = values[0].strip() if values else ""
    if spec.kind == KIND_INT:
        if not raw:
            return None if spec.optional else spec.default
        try:
            return int(raw)
        except ValueError:
            raise ValueError("must be a whole number") from None
    if not raw and spec.optional:
        return None
    return raw


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


def decode_secrets(
    names: Sequence[str],
    form: Mapping[str, list[str]],
    source_of: Callable[[str], str],
) -> tuple[dict[str, str], list[str]]:
    """(values to set, names to clear) from `secret.<name>` and `clear.<name>`.

    A name whose source is "env" is skipped entirely: the env var wins at read
    time, so storing a value would be invisible and misleading. A blank field
    means "leave alone" — clearing is explicit, via the checkbox."""
    to_set: dict[str, str] = {}
    to_clear: list[str] = []
    for name in names:
        if source_of(name) == "env":
            continue
        if form.get(f"clear.{name}"):
            to_clear.append(name)
            continue
        values = form.get(f"secret.{name}", [])
        value = values[0].strip() if values else ""
        if value:
            to_set[name] = value
    return to_set, to_clear


def errors_by_path(
    exc: SettingsInvalid, *, known: Iterable[str]
) -> tuple[dict[str, str], list[str]]:
    """Split validation errors into ones an input can show and ones it cannot.

    Model-level validators (the crontab checks, the remote_policy "not both"
    rule) produce locs no input owns; those belong in the form-level banner
    rather than being dropped."""
    known = set(known)
    by_path: dict[str, str] = {}
    form_level: list[str] = []
    for err in exc.errors:
        loc, msg = err.get("loc", ""), err["msg"]
        if loc in known:
            by_path.setdefault(loc, msg)
        else:
            form_level.append(f"{loc}: {msg}" if loc else msg)
    return by_path, form_level
