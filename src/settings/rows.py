"""A `list[BaseModel]` settings field as addressable rows.

A row is addressed by a digest of its canonical JSON, not by its index: two
browser tabs, or a tab and a `--merge` script, shift a list under each other,
and an index-addressed edit would then rewrite a different entry. A digest
that has gone raises RowGone, which the caller turns into "that entry is no
longer configured".

Every mutation returns a dotted-path patch whose single value is the WHOLE new
list, which is what ConfigService.update_settings applies. Nothing here
touches a service, a store, or a request."""
from __future__ import annotations

import hashlib
import json
import typing
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from src.config import AppConfig
from src.settings.errors import SettingsInvalid
from src.settings.fields import (
    KIND_CHIPS, SCALAR_ITEM_FIELD, _resolve, field_map, item_fields, item_model, rows_paths,
    value_at,
)

_DIGEST_BYTES = 6  # 12 hex characters: short enough for a URL, wide enough here


class RowGone(Exception):
    """The addressed row is not in the list any more (someone else edited it)."""


class NotRowsPath(Exception):
    """The dotted path is not a list-of-model settings field."""


@dataclass(frozen=True)
class Row:
    digest: str
    values: dict[str, Any]   # item-relative field name -> JSON-safe value
    entry: Any               # the stored form: a dict, or a bare str for a union list


def row_digest(entry: Any) -> str:
    """A stable short digest of an entry's canonical JSON. Key order cannot
    change it, so a row keeps its address across a re-render."""
    blob = json.dumps(entry, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.blake2s(blob.encode("utf-8"), digest_size=_DIGEST_BYTES).hexdigest()


def _check(path: str) -> type[BaseModel] | None:
    """The element model for a rows path, or None for a scalar (chips) list.
    Raises NotRowsPath for anything else."""
    if path in rows_paths():
        return item_model(path)
    spec = field_map().get(path)
    if spec is not None and spec.kind == KIND_CHIPS:
        return None
    raise NotRowsPath(f"{path} is not a list settings field")


def _stored_entries(cfg: AppConfig, path: str) -> list[Any]:
    """The list as it is stored: dicts, plus bare strings for a union list."""
    out: list[Any] = []
    for item in value_at(cfg, path) or []:
        out.append(item.model_dump(mode="json") if isinstance(item, BaseModel) else item)
    return out


def _as_values(model: type[BaseModel] | None, entry: Any) -> dict[str, Any]:
    """An entry as {field name: value} for every field of the element model.

    A scalar list has no model: its single synthetic field holds the string.
    A bare string in a union list (hiringcafe.extra_queries) is the model's
    first field; the rest read as unset, which is what the form should show."""
    if model is None:
        return {SCALAR_ITEM_FIELD: entry}
    if isinstance(entry, dict):
        return {name: entry.get(name) for name in model.model_fields}
    first, *rest = model.model_fields
    return {first: entry, **{name: None for name in rest}}


def _blank_optionals_to_none(path: str, values: dict[str, Any]) -> dict[str, Any]:
    """A blank text input for an optional item field (hiringcafe.location,
    *.company, ...) is the form's way of saying "unset", the same convention
    src/web/settings/forms.py._value uses for a section field. Left as "" it
    would reach the element model's validator as a real value — for
    HiringCafeSearch.location that raises "unknown hiring.cafe location ''"
    instead of quietly clearing the field, which is what breaks writing a
    location-less query back as a bare string."""
    optional_names = {f.name for f in item_fields(path) if f.optional}
    return {
        name: (None if name in optional_names and isinstance(v, str) and not v.strip() else v)
        for name, v in values.items()
    }


def _as_entry(path: str, model: type[BaseModel] | None, values: dict[str, Any]) -> Any:
    """The stored form of a submitted row.

    Validation happens here rather than at save time so the error can name the
    item-relative field the form input is called after — SettingsInvalid with
    loc="flavor", which the web layer places under `item.flavor`. A raw
    pydantic ValidationError would carry a tuple loc no input owns and land in
    the form-level banner instead.

    A union list whose entry has only its first field set is written back as a
    bare string, which is the shape those lists already hold and the shape the
    connector reads."""
    if model is None:
        value = str(values.get(SCALAR_ITEM_FIELD, "")).strip()
        if not value:
            raise SettingsInvalid([{"loc": SCALAR_ITEM_FIELD, "msg": "must not be empty"}])
        return value
    cleaned = _blank_optionals_to_none(path, values)
    try:
        validated = model.model_validate(cleaned)
    except ValidationError as exc:
        raise SettingsInvalid([
            {"loc": ".".join(str(p) for p in e["loc"]), "msg": e["msg"]} for e in exc.errors()
        ]) from exc
    entry = validated.model_dump(mode="json", exclude_defaults=True)
    first, *rest = model.model_fields
    if _is_union_list(model) and set(entry) <= {first}:
        return entry.get(first, "")
    return entry


def _is_union_list(model: type[BaseModel] | None) -> bool:
    """Whether this element model also has a bare-string form in its list."""
    return model in _UNION_ITEM_MODELS


# Element models whose list is annotated `list[str | Model]`. Derived once from
# the config module rather than hardcoded by name, so a second union list added
# later behaves the same without editing this file.
#
# src/config.py starts with `from __future__ import annotations`, so a field's
# annotation can arrive as a bare string or ForwardRef rather than a real type
# object (see fields._resolve's own docstring — it exists for exactly this).
# Reading `field.annotation` raw and matching it against `list` / union members
# would then silently find nothing, leaving this frozenset permanently empty
# and hiringcafe.extra_queries entries never written back as bare strings.
# Resolving every annotation through fields._resolve before inspecting it
# avoids that; it is a documented no-op on an annotation that is already a
# real type, so this is safe however pydantic happened to resolve a given
# field in this pass.
def _union_item_models() -> frozenset[type[BaseModel]]:
    import src.config as config_module

    found: set[type[BaseModel]] = set()
    seen: set[type[BaseModel]] = set()

    def scan(model: type[BaseModel]) -> None:
        if model in seen:
            return
        seen.add(model)
        for field in model.model_fields.values():
            ann = _resolve(field.annotation)
            if typing.get_origin(ann) is list:
                (item,) = typing.get_args(ann) or (str,)
                members = [_resolve(m) for m in typing.get_args(_resolve(item))]
                if str in members:
                    for member in members:
                        if isinstance(member, type) and issubclass(member, BaseModel):
                            found.add(member)
                continue
            # Not a list: still walk into a nested model (or Model | None) so a
            # union list buried under a sub-config is found too.
            candidates = [_resolve(a) for a in typing.get_args(ann) if a is not type(None)]
            if not candidates and isinstance(ann, type):
                candidates = [ann]
            for candidate in candidates:
                if isinstance(candidate, type) and issubclass(candidate, BaseModel):
                    scan(candidate)

    scan(config_module.AppConfig)
    return frozenset(found)


_UNION_ITEM_MODELS = _union_item_models()


def list_rows(cfg: AppConfig, path: str) -> list[Row]:
    model = _check(path)
    return [
        Row(digest=row_digest(entry), values=_as_values(model, entry), entry=entry)
        for entry in _stored_entries(cfg, path)
    ]


def find_row(cfg: AppConfig, path: str, digest: str) -> Row:
    for row in list_rows(cfg, path):
        if row.digest == digest:
            return row
    raise RowGone(f"{path}: no entry with id {digest}")


def add_row_patch(cfg: AppConfig, path: str, values: dict[str, Any]) -> dict[str, list]:
    model = _check(path)
    return {path: [*_stored_entries(cfg, path), _as_entry(path, model, values)]}


def update_row_patch(
    cfg: AppConfig, path: str, digest: str, values: dict[str, Any],
) -> dict[str, list]:
    model = _check(path)
    entries = _stored_entries(cfg, path)
    for i, entry in enumerate(entries):
        if row_digest(entry) == digest:
            return {path: [*entries[:i], _as_entry(path, model, values), *entries[i + 1:]]}
    raise RowGone(f"{path}: no entry with id {digest}")


def remove_row_patch(cfg: AppConfig, path: str, digest: str) -> dict[str, list]:
    _check(path)
    entries = _stored_entries(cfg, path)
    for i, entry in enumerate(entries):
        if row_digest(entry) == digest:
            return {path: [*entries[:i], *entries[i + 1:]]}
    raise RowGone(f"{path}: no entry with id {digest}")
