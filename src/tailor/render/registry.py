"""Template pack discovery: built-in packs shipped in the repo merged with
user packs under resume/templates/ (bind-mounted; uploads land there). A pack
is a directory holding template.html.j2 (+ optional meta.yaml and fonts/)."""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

BUILTIN_DIR = Path(__file__).parent / "templates"
DEFAULT_SLUG = "classic"
TEMPLATE_FILENAME = "template.html.j2"

# Pack slugs are directory names made by the builder's slugify(); anything else
# (dots, slashes, uppercase, empty) in a request is crafted, not a template.
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


@dataclass(frozen=True)
class TemplateInfo:
    slug: str
    name: str
    description: str
    source: str  # builtin | upload | docx-import
    path: Path


def user_templates_dir() -> Path:
    return Path(os.environ.get("JOB_AGG_TEMPLATES_DIR", "resume/templates"))


def _load_meta(d: Path) -> dict:
    meta = d / "meta.yaml"
    if not meta.exists():
        return {}
    try:
        return yaml.safe_load(meta.read_text()) or {}
    except (OSError, yaml.YAMLError) as exc:
        log.warning("template_meta_unreadable", extra={"pack": d.name, "error": str(exc)})
        return {}


def pack_info(path: Path, *, source: str) -> TemplateInfo:
    meta = _load_meta(path)
    return TemplateInfo(
        slug=path.name,
        name=str(meta.get("name", path.name)),
        description=str(meta.get("description", "")),
        source=str(meta.get("source", source)),
        path=path,
    )


def _scan(root: Path, *, source: str) -> list[TemplateInfo]:
    if not root.is_dir():
        return []
    return [
        pack_info(d, source=source)
        for d in sorted(root.iterdir())
        if d.is_dir() and not d.name.startswith(".") and (d / TEMPLATE_FILENAME).exists()
    ]


def list_templates() -> list[TemplateInfo]:
    return _scan(BUILTIN_DIR, source="builtin") + _scan(user_templates_dir(), source="upload")


def builtin_slugs() -> set[str]:
    return {t.slug for t in _scan(BUILTIN_DIR, source="builtin")}


def get_template(slug: str) -> TemplateInfo:
    for t in list_templates():
        if t.slug == slug:
            return t
    log.warning("template_missing_falling_back", extra={"slug": slug})
    # classic ships inside the package, so this next() always finds it
    return next(t for t in _scan(BUILTIN_DIR, source="builtin") if t.slug == DEFAULT_SLUG)
