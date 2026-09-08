"""Builder settings: the user-editable knobs for résumé rendering. Persisted as
one JSON row in SQLite (SqliteBuilderSettingsStore); validated here so the web
form and the store agree on what's legal."""

from __future__ import annotations

import logging
import re
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

log = logging.getLogger(__name__)

_MARGINS_RE = re.compile(r"^\s*\d*\.?\d+(pt|in|cm|mm)(\s+\d*\.?\d+(pt|in|cm|mm)){0,3}\s*$")


class BuilderSettings(BaseModel):
    active_template: str = "classic"
    max_bullets_per_experience: int | None = Field(default=None, ge=1)
    max_bullets_per_project: int | None = Field(default=None, ge=1)
    min_bullets_per_entry: int = Field(default=1, ge=1)
    max_pages: int = Field(default=1, ge=1)
    page_size: Literal["letter", "a4"] | None = None
    margins: str | None = None  # CSS shorthand, 1-4 lengths (pt/in/cm/mm)

    @field_validator("margins")
    @classmethod
    def _margins_css(cls, v: str | None) -> str | None:
        if v is not None and not _MARGINS_RE.match(v):
            raise ValueError("margins must be 1-4 CSS lengths (pt/in/cm/mm), e.g. '0.5in 0.58in'")
        return v

    @model_validator(mode="after")
    def _caps_at_least_min(self) -> "BuilderSettings":
        for cap in (self.max_bullets_per_experience, self.max_bullets_per_project):
            if cap is not None and cap < self.min_bullets_per_entry:
                raise ValueError("bullet caps must be >= min_bullets_per_entry")
        return self


def settings_from_dict(d: dict | None) -> BuilderSettings:
    """Fail-soft loader for store data: anything invalid falls back to defaults
    (a corrupt settings row must never break a render)."""
    try:
        return BuilderSettings(**(d or {}))
    except (ValidationError, TypeError) as exc:
        log.warning("builder_settings_invalid_falling_back", extra={"error": str(exc)})
        return BuilderSettings()


def page_setup_css(s: BuilderSettings) -> str | None:
    """@page override stylesheet for the user's page size/margins, or None when
    the template's own @page rule should stand. Declarations are marked
    `!important`: WeasyPrint applies stylesheets passed via `render_html_to_pdf`'s
    `extra_css` at 'user' cascade origin, which loses to a pack's own `<style>`
    (author origin) at equal specificity unless flagged important — so without
    it, a pack that hardcodes `@page { size: letter }` (as classic does) could
    never be overridden by the user's settings."""
    parts = []
    if s.page_size:
        parts.append(f"size: {s.page_size} !important")
    if s.margins:
        parts.append(f"margin: {s.margins} !important")
    return "@page { " + "; ".join(parts) + " }" if parts else None
