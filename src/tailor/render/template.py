"""Render a RenderDoc to HTML via a template pack. Every pack — built-in or
uploaded — renders in an immutable sandbox with autoescape: uploaded template
code is still code."""

from __future__ import annotations

from pathlib import Path

from jinja2 import FileSystemLoader
from jinja2.sandbox import ImmutableSandboxedEnvironment

from src.tailor.render.assemble import RenderDoc
from src.tailor.render.registry import DEFAULT_SLUG, TEMPLATE_FILENAME, TemplateInfo, get_template

_envs: dict[str, ImmutableSandboxedEnvironment] = {}


def _env_for(path: Path) -> ImmutableSandboxedEnvironment:
    key = str(path)
    if key not in _envs:
        _envs[key] = ImmutableSandboxedEnvironment(
            loader=FileSystemLoader(key),
            autoescape=True,  # escape ALL content variables (files are *.j2)
        )
    return _envs[key]


def render_html(doc: RenderDoc, pack: TemplateInfo | None = None) -> str:
    pack = pack or get_template(DEFAULT_SLUG)
    return _env_for(pack.path).get_template(TEMPLATE_FILENAME).render(doc=doc)
