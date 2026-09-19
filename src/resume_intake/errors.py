"""The exception the résumé-intake drafters raise.

A leaf module with no imports of its own, on purpose. `DraftFailed` used to
live in draft.py, which reaches into `src.web.settings.forms` for `apply_patch`
— so every module that wanted only the exception pulled the whole web settings
package in behind it. `src/resume_intake/content_draft.py` doing exactly that
is what forced `src/web/settings/routes.py` to import its own `content_draft`
sibling lazily: with `src.resume_intake.draft` as the entry point, draft.py
imported `src.web.settings.forms`, that ran this repo's `src/web/settings/`
`__init__` (`from src.web.settings.routes import register_settings_routes`),
routes.py imported `src.web.settings.content_draft`, and that came back round
to `src.resume_intake.draft` — still mid-import, with `DraftFailed` not yet
defined.

Keeping the exception here lets a caller that only needs to catch it stay
clear of that loop entirely. draft.py's own web-layer dependency is untouched
and still an inversion; this only stops it being contagious.
"""
from __future__ import annotations


class DraftFailed(Exception):
    """User-facing reason the draft could not be produced."""
