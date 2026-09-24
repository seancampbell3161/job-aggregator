"""The shared settings-page shell: the context every section template
expects (page_ctx), what a secrets list renders from (secret_rows), and the
thin renderer around them that returns a full section page (render_section).

Split out of routes.py per Ruling R10: three leaf route modules — rows.py
(its own row-page renderer, left alone, see rows.py's own docstring),
companies.py, and this task's history.py — all needed the same page shell,
and companies.py had been reaching back into routes.py (`from
src.web.settings.routes import _render`) to get it, which forced routes.py
to import companies.py lazily inside register_settings_routes to dodge the
resulting cycle. Moving the shell here removes the cycle: routes.py, like
every other leaf module, now imports FROM this module, and no leaf route
module imports from routes.py at all.

_SINK_PROBES itself stays in routes.py, deliberately: it maps a {probe} URL
slug to the secret it tests and the probe function to run, and
tests/web/settings/test_notifications_section.py monkeypatches
src.web.settings.routes.probe_ntfy / probe_discord by that module path —
moving the registry risks quietly breaking how those tests reach it. So
page_ctx takes the probe-target mapping (secret name -> {probe} slug) as a
parameter instead of computing it itself; routes.py derives it from
_SINK_PROBES fresh on every call (so a test that monkeypatches _SINK_PROBES
after the app is built still sees it take effect) and passes it in. A caller
with no per-secret Test buttons (companies.py, history.py — neither section
claims any secrets) simply omits it and gets none."""
from __future__ import annotations

from typing import Mapping

from fastapi import Request
from fastapi.responses import HTMLResponse

from src.settings.copy import secret_label
from src.settings.service import secret_env_var
from src.web.settings.sections import SECTIONS, section_fields

# Secrets masked as type="password" — a name ending in one of these never
# renders its stored value either way, so masking only affects what the user
# can see while typing or pasting a new one. That's worth it for an API key,
# but not for a pasted URL or identifier (ntfy topic, Discord webhook,
# tailor_endpoint_url, ...): those can't be proofread before submit if
# masked, and several of their pages (integrations) have no Test button, so a
# typo fails silently until the feature breaks.
_MASKED_SECRET_SUFFIXES = ("_key", "_password", "_secret")


def secret_rows(service, names) -> list[dict]:
    """{name, source, env_var, label, masked} for each of a section's secrets
    — the template never sees the value, only where it currently comes from."""
    return [
        {
            "name": name,
            "source": service.secret_source(name),
            "env_var": secret_env_var(name),
            "label": secret_label(name),
            "masked": name.endswith(_MASKED_SECRET_SUFFIXES),
        }
        for name in names
    ]


def page_ctx(
    request: Request, section, *, probe_targets: Mapping[str, str] | None = None, **extra,
) -> dict:
    """The context every section template expects. Values render from the
    validated AppConfig on the request snapshot, never from the stored doc —
    the doc is sparse (canonical_doc drops defaults), the config is complete."""
    ctx = {
        "sections": SECTIONS,
        "section": section,
        "cfg": request.state.snapshot.cfg,
        "fields": section_fields(section) if section.paths else (),
        "secrets": secret_rows(request.app.state.service, section.secrets),
        "errors": {},
        "form_errors": [],
        "saved": False,
        "submitted": None,
        # Secret name -> the {probe} slug it tests, for secret_field's
        # per-secret Test button. The caller (routes.py) derives this from
        # its own _SINK_PROBES registry so the two can't drift apart; a
        # section with no probes of its own just leaves this out.
        "probe_targets": dict(probe_targets or {}),
    }
    ctx.update(extra)
    return ctx


def render_section(request: Request, section, **extra) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request, section.template, page_ctx(request, section, **extra)
    )
