"""Home: whether the app is working, and what a newcomer should do next.

Nothing here is stored except the "hide getting started" flag — every
checklist item is derived from data the app already has, so the page can't
drift from reality. Every read is fail-soft: Home is where a newcomer lands,
so it must never 500."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse

log = logging.getLogger(__name__)

# Key in the non-versioned wizard_ui KV store (SqliteWizardStore).
HIDDEN_KEY = "getting_started_hidden"


@dataclass(frozen=True)
class ChecklistItem:
    key: str
    label: str
    done: bool
    href: str
    action: str
    blocked: bool = False


@dataclass(frozen=True)
class GettingStarted:
    items: tuple[ChecklistItem, ...]
    hidden: bool

    @property
    def complete(self) -> bool:
        return all(item.done for item in self.items)

    @property
    def active(self) -> bool:
        return not self.complete and not self.hidden


def _safe(key: str, fn: Callable[[], bool]) -> bool:
    try:
        return bool(fn())
    except Exception as exc:  # noqa: BLE001 — one item degrades, the checklist survives
        log.warning("getting_started_item_unavailable", extra={"item": key, "error": str(exc)})
        return False


def getting_started(request: Request) -> GettingStarted:
    state = request.app.state

    def search() -> bool:
        from src.web.wizard.routes import current_context
        from src.web.wizard.steps import next_step
        return next_step(current_context(request), state.stores.wizard.skipped()) is None

    def first_check() -> bool:
        live = state.ops.liveness()
        return live is not None and live.last_success_ms is not None

    def review() -> bool:
        return any(status != "new" and n for status, n in state.repo.status_counts().items())

    def alerts() -> bool:
        source = state.service.secret_source
        return source("ntfy_topic_url") != "unset" or source("discord_webhook_url") != "unset"

    checked = _safe("first_check", first_check)
    items = (
        ChecklistItem("search", "Set up your search", _safe("search", search),
                      "/wizard", "Finish setup"),
        ChecklistItem("first_check", "First check finished", checked,
                      "/pipeline", "See System health"),
        ChecklistItem("review", "Review your first matches", _safe("review", review),
                      "/", "Open Matches", blocked=not checked),
        ChecklistItem("alerts", "Add phone alerts", _safe("alerts", alerts),
                      "/settings/notifications", "Set up alerts"),
    )
    hidden = _safe("hidden", lambda: state.stores.wizard.get(HIDDEN_KEY) is not None)
    return GettingStarted(items=items, hidden=hidden)


def landing_url(request: Request) -> str:
    """Where the app sends someone on its own (login without a next, the
    brand link, the wizard's done page): Home while getting started is
    unfinished and not hidden, then Matches. Before setup there is no
    snapshot and the setup gate owns every page, so "/" is as good as any."""
    if getattr(request.state, "snapshot", None) is None:
        return "/"
    return "/home" if getting_started(request).active else "/"


def register_home_routes(app: FastAPI) -> None:
    @app.get("/home", response_class=HTMLResponse)
    def home(request: Request):
        return request.app.state.templates.TemplateResponse(
            request, "home.html", {"gs": getting_started(request)},
        )

    @app.post("/home/getting-started/hide")
    def hide(request: Request):
        request.app.state.stores.wizard.put(HIDDEN_KEY, True)
        return RedirectResponse("/home", status_code=303)

    @app.post("/home/getting-started/show")
    def show(request: Request):
        request.app.state.stores.wizard.delete(HIDDEN_KEY)
        return RedirectResponse("/home", status_code=303)
