"""The app's navigation, defined once. base.html renders it via
_sidebar.html; the active item comes from the request path, so no page has
to declare which one it is. URLs here are the long-standing routes —
bookmarks and ntfy/Discord deep links depend on them; only labels changed
(spec: docs/superpowers/specs/2026-09-23-app-shell-home-design.md)."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

from fastapi import Request

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class NavItem:
    label: str
    href: str
    prefixes: tuple[str, ...]          # paths that make this item active
    visible: Callable[[Request], bool] | None = None
    badge: Callable[[Request], int | None] | None = None


@dataclass(frozen=True)
class NavGroup:
    label: str | None                  # None = ungrouped (Home)
    items: tuple[NavItem, ...]


@dataclass(frozen=True)
class NavLink:
    label: str
    href: str
    current: bool
    badge: int | None


def _new_matches(request: Request) -> int | None:
    """Matches still marked new. None (no badge) on zero or on any error —
    this is on every page, and page chrome must never 500 a page."""
    try:
        n = request.app.state.repo.status_counts().get("new", 0)
    except Exception as exc:  # noqa: BLE001
        log.warning("nav_badge_unavailable", extra={"error": str(exc)})
        return None
    return n or None


def _coach_visible(request: Request) -> bool:
    from src.web import coach  # module lookup, so tests can patch it
    return coach.coach_nav_visible(request)


def _signed_in(request: Request) -> bool:
    return bool(getattr(request.state, "session", None))


NAV: tuple[NavGroup, ...] = (
    NavGroup(None, (NavItem("Home", "/home", ("/home",)),)),
    NavGroup("Job search", (
        NavItem("Matches", "/", ("/", "/jobs", "/detail"), badge=_new_matches),
        NavItem("Applications", "/board", ("/board",)),
        NavItem("Apply kit", "/kit", ("/kit",)),
    )),
    NavGroup("Résumé", (
        NavItem("Résumé templates", "/builder", ("/builder",)),
    )),
    NavGroup("Insights", (
        NavItem("Progress", "/analytics", ("/analytics",)),
        NavItem("Coach", "/coach", ("/coach",), visible=_coach_visible),
        NavItem("System health", "/pipeline", ("/pipeline",)),
        NavItem("Rejected postings", "/audit", ("/audit",)),
    )),
)

NAV_FOOTER: tuple[NavItem, ...] = (
    NavItem("Settings", "/settings", ("/settings",)),
    NavItem("Account", "/account/password", ("/account",), visible=_signed_in),
)


def _all_items() -> list[NavItem]:
    return [item for group in NAV for item in group.items] + list(NAV_FOOTER)


def _prefix_matches(path: str, prefix: str) -> bool:
    if prefix == "/":
        return path == "/"
    return path == prefix or path.startswith(prefix + "/")


def active_href(path: str) -> str | None:
    """The nav item for this path: longest matching prefix, at a "/"
    boundary (the same rule as the setup gate's exemptions)."""
    best, best_len = None, -1
    for item in _all_items():
        for prefix in item.prefixes:
            if _prefix_matches(path, prefix) and len(prefix) > best_len:
                best, best_len = item.href, len(prefix)
    return best


def nav_context(request: Request) -> dict:
    active = active_href(request.url.path)

    def links(items) -> list[NavLink]:
        return [
            NavLink(item.label, item.href, item.href == active,
                    item.badge(request) if item.badge else None)
            for item in items
            if item.visible is None or item.visible(request)
        ]

    groups = [{"label": g.label, "links": links(g.items)} for g in NAV]
    return {"groups": [g for g in groups if g["links"]], "footer": links(NAV_FOOTER)}
