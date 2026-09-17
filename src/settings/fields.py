"""The AppConfig model as data: one FieldSpec per leaf path.

Both the generated Advanced pages and the hand-built settings sections render
from this, so a flag added to src/config.py surfaces in the UI without a
template change. tests/test_config_docs.py walks the same function, so the docs
guard and the UI can never disagree about what a flag is."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from functools import lru_cache
from typing import Any, Iterator, Literal, get_args, get_origin
from zoneinfo import ZoneInfo

from pydantic import BaseModel
from pydantic_core import PydanticUndefined

import src.config as config_module
from src.config import AppConfig

KIND_BOOL = "bool"
KIND_INT = "int"
KIND_TEXT = "text"
KIND_TIME = "time"
KIND_CHOICE = "choice"
KIND_MULTI_CHOICE = "multi_choice"
KIND_CHIPS = "chips"
KIND_READ_ONLY = "read_only"

# Walked (so the docs guard sees them) but never offered as a settings input:
# secrets are not part of the settings document at all — they are written with
# ConfigService.set_secret — and a deprecated key the validator pops would be
# re-added by a form round-trip.
EXCLUDED_ROOTS = frozenset({"secrets"})
EXCLUDED_PATHS = frozenset({"filters.location.remote_must_be_us"})


@dataclass(frozen=True)
class FieldSpec:
    path: str
    kind: str
    default: Any = None
    optional: bool = False
    editable: bool = True
    ge: int | None = None
    le: int | None = None
    choices: tuple[str, ...] = ()

    @property
    def name(self) -> str:
        return self.path.rsplit(".", 1)[-1]

    @property
    def root(self) -> str:
        return self.path.split(".", 1)[0]

    @property
    def label(self) -> str:
        return self.name.replace("_", " ").capitalize()


def _resolve(ann: Any) -> Any:
    """Resolve a string/ForwardRef annotation against src.config's namespace.

    src/config.py uses `from __future__ import annotations`, so a field declared
    with a quoted type surfaces as ForwardRef("'LocationFilterConfig'") — nested
    quotes — hence the strip. Unresolvable names come back unchanged."""
    name = ann if isinstance(ann, str) else getattr(ann, "__forward_arg__", None)
    if name is None:
        return ann
    return getattr(config_module, name.strip("'\""), ann)


def _is_model(ann: Any) -> bool:
    return isinstance(ann, type) and issubclass(ann, BaseModel)


def _unwrap_optional(ann: Any) -> tuple[Any, bool]:
    """(inner annotation, was_optional) for `T | None`; unchanged otherwise."""
    args = [a for a in get_args(ann) if a is not type(None)]
    if len(args) < len(get_args(ann)) and len(args) == 1:
        return _resolve(args[0]), True
    return ann, False


def _bounds(field: Any) -> tuple[int | None, int | None]:
    ge = le = None
    for meta in field.metadata:
        ge = getattr(meta, "ge", None) if getattr(meta, "ge", None) is not None else ge
        le = getattr(meta, "le", None) if getattr(meta, "le", None) is not None else le
    return ge, le


def _default(field: Any) -> Any:
    if field.default is not PydanticUndefined:
        return field.default
    if field.default_factory is not None:
        value = field.default_factory()
        if isinstance(value, list):
            return tuple(value)
        return value
    return None


def _scalar_kind(ann: Any) -> str | None:
    if ann is bool:
        return KIND_BOOL
    if ann is int:
        return KIND_INT
    if ann is str:
        return KIND_TEXT
    if ann is time:
        return KIND_TIME
    if ann is ZoneInfo:
        return KIND_TEXT
    if get_origin(ann) is Literal:
        return KIND_CHOICE
    return None


def _list_kind(ann: Any) -> tuple[str, tuple[str, ...]]:
    """Kind and choices for a `list[...]` annotation."""
    (item,) = get_args(ann) or (str,)
    item = _resolve(item)
    if get_origin(item) is Literal:
        return KIND_MULTI_CHOICE, tuple(str(c) for c in get_args(item))
    if item is str:
        return KIND_CHIPS, ()
    return KIND_READ_ONLY, ()  # list[Model], or a mixed union — 3b territory


def _walk(model: type[BaseModel], prefix: str = "") -> Iterator[FieldSpec]:
    for name, field in model.model_fields.items():
        path = f"{prefix}{name}"
        ann, optional = _unwrap_optional(_resolve(field.annotation))
        ann = _resolve(ann)

        if _is_model(ann):
            yield from _walk(ann, f"{path}.")
            continue

        editable = path not in EXCLUDED_PATHS and path.split(".", 1)[0] not in EXCLUDED_ROOTS
        if get_origin(ann) is list:
            kind, choices = _list_kind(ann)
            ge = le = None
        else:
            kind = _scalar_kind(ann) or KIND_TEXT
            choices = tuple(str(c) for c in get_args(ann)) if kind == KIND_CHOICE else ()
            ge, le = _bounds(field)

        default = _default(field)
        if isinstance(default, (time, ZoneInfo)):
            default = str(default)
        yield FieldSpec(
            path=path, kind=kind, default=default, optional=optional,
            editable=editable, ge=ge, le=le, choices=choices,
        )


@lru_cache(maxsize=1)
def field_map() -> dict[str, FieldSpec]:
    """Every leaf of the AppConfig tree, in declaration order."""
    return {f.path: f for f in _walk(AppConfig)}


@lru_cache(maxsize=1)
def all_paths() -> frozenset[str]:
    return frozenset(field_map())


@lru_cache(maxsize=1)
def editable_fields() -> tuple[FieldSpec, ...]:
    return tuple(f for f in field_map().values() if f.editable)


def value_at(root: object, path: str) -> Any:
    """Read a dotted path off a validated AppConfig.

    Returns None when a parent optional group is unset (quiet_hours), so a
    template can render an empty field instead of raising."""
    node: Any = root
    for part in path.split("."):
        if node is None:
            return None
        node = getattr(node, part, None)
    if isinstance(node, (time, ZoneInfo)):
        return str(node)
    if isinstance(node, (list, tuple)):
        return [str(v) if isinstance(v, (time, ZoneInfo)) else v for v in node]
    return node


@lru_cache(maxsize=1)
def optional_groups() -> frozenset[str]:
    """Dotted paths of `BaseModel | None` fields — groups the UI renders behind
    an enable toggle (today: quiet_hours)."""
    out: set[str] = set()

    def scan(model: type[BaseModel], prefix: str = "") -> None:
        for name, field in model.model_fields.items():
            path = f"{prefix}{name}"
            ann, optional = _unwrap_optional(_resolve(field.annotation))
            ann = _resolve(ann)
            if _is_model(ann):
                if optional:
                    out.add(path)
                scan(ann, f"{path}.")

    scan(AppConfig)
    return frozenset(out)
