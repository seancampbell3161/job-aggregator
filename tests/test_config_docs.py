"""Drift guard: every config flag has a row in docs/CONFIG.md, and every
documented dotted path still exists in the model tree. This is the mechanism
that keeps the reference honest — new flags fail CI until documented."""
import re
from pathlib import Path

from pydantic import BaseModel

import src.config as config_module
from src.config import AppConfig

DOC_PATH = Path(__file__).resolve().parent.parent / "docs" / "CONFIG.md"


def _resolve(ann):
    """Resolve string/ForwardRef annotations against src.config's namespace.

    With `from __future__ import annotations`, a field declared with a quoted
    type (location: "LocationFilterConfig") surfaces in model_fields as
    ForwardRef("'LocationFilterConfig'") — nested quotes — so we strip quote
    chars before the lookup. Unresolvable names return unchanged."""
    name = None
    if isinstance(ann, str):
        name = ann
    else:
        fw = getattr(ann, "__forward_arg__", None)
        if fw is not None:
            name = fw
    if name is None:
        return ann
    return getattr(config_module, name.strip("'\""), ann)


def _walk(model: type[BaseModel], prefix: str = "") -> set[str]:
    """Dotted paths of every leaf field. Nested BaseModels descend;
    list[...] fields, scalars, and scalar unions are leaves."""
    paths: set[str] = set()
    for name, field in model.model_fields.items():
        ann = _resolve(field.annotation)
        nested = None
        if isinstance(ann, type) and issubclass(ann, BaseModel):
            nested = ann
        elif getattr(ann, "__origin__", None) is not list:
            # unions: Model | None descends; scalar unions are leaves
            for arg in getattr(ann, "__args__", ()):
                arg = _resolve(arg)
                if isinstance(arg, type) and issubclass(arg, BaseModel):
                    nested = arg
                    break
        if nested is not None:
            paths |= _walk(nested, f"{prefix}{name}.")
        else:
            paths.add(f"{prefix}{name}")
    return paths


_DOTTED_IN_DOC = re.compile(r"`([a-z_]+(?:\.[a-z_0-9]+)+)`")


def test_every_config_flag_is_documented():
    paths = _walk(AppConfig)
    # canary: the ForwardRef'd location gate must have been descended into
    assert "filters.location.allowed_countries" in paths
    doc = DOC_PATH.read_text()
    missing = sorted(p for p in paths if p not in doc)
    assert not missing, (
        "config flags missing from docs/CONFIG.md — add a table row for each:\n  "
        + "\n  ".join(missing)
    )


def test_no_stale_doc_rows():
    paths = _walk(AppConfig)
    sections = set(AppConfig.model_fields)
    doc = DOC_PATH.read_text()
    documented = {
        m.group(1)
        for m in _DOTTED_IN_DOC.finditer(doc)
        if m.group(1).split(".")[0] in sections
    }
    stale = sorted(documented - paths)
    assert not stale, (
        "docs/CONFIG.md documents config paths that no longer exist "
        "(flag deleted or renamed?):\n  " + "\n  ".join(stale)
    )
