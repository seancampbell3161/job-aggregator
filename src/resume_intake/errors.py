"""The exception the résumé-intake drafters raise.

A leaf module with no imports of its own, on purpose, so a caller that only
needs to catch the exception imports nothing else. `DraftFailed` used to live
in draft.py back when draft.py reached into `src.web.settings.forms` for
`apply_patch`, and that closed an import cycle: with `src.resume_intake.draft`
as the entry point, draft.py imported `src.web.settings.forms`, that ran
`src/web/settings/__init__` (`from src.web.settings.routes import
register_settings_routes`), routes.py imported `src.web.settings.content_draft`,
and that came back round to `src.resume_intake.draft` — still mid-import, with
`DraftFailed` not yet defined. draft.py no longer touches the web layer
(tests/resume_intake/test_layering.py keeps it that way), but a leaf is still
the right home for an exception.
"""
from __future__ import annotations


class DraftFailed(Exception):
    """User-facing reason the draft could not be produced."""
