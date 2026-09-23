"""Home: whether the app is working, and what a newcomer should do next.

Nothing here is stored except the "hide getting started" flag — every
checklist item is derived from data the app already has, so the page can't
drift from reality. Every read is fail-soft: Home is where a newcomer lands,
so it must never 500."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from src.ops_alerts import stale_threshold_minutes
from src.web.ops import Liveness
from src.web.pipeline_activity import format_ago

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


@dataclass(frozen=True)
class HomeStatus:
    state: str                  # waiting | running | stalled | unavailable
    last_ago: str | None
    next_eta: str | None
    interval_minutes: int


def _eta(ms: int) -> str:
    if ms <= 60_000:
        return "any minute now"
    minutes = -(-ms // 60_000)   # ceil
    if minutes < 60:
        return f"in about {minutes} min"
    hours, rest = divmod(minutes, 60)
    return f"in about {hours} h {rest} min" if rest else f"in about {hours} h"


def status_line(liveness: Liveness | None, *, ats_minutes: int, now_ms: int) -> HomeStatus:
    """The one headline Home shows. Stalled uses the pipeline-stopped
    watchdog's own threshold, so Home and that alert never disagree."""
    if liveness is None:
        return HomeStatus("unavailable", None, None, ats_minutes)
    if liveness.last_cycle_ms is None:
        return HomeStatus("waiting", None, None, ats_minutes)
    ago = format_ago(liveness.last_cycle_ms, now_ms)
    if now_ms - liveness.last_cycle_ms > stale_threshold_minutes(ats_minutes) * 60_000:
        return HomeStatus("stalled", ago, None, ats_minutes)
    anchor = liveness.last_ats_ms if liveness.last_ats_ms is not None else liveness.last_cycle_ms
    return HomeStatus("running", ago, _eta(anchor + ats_minutes * 60_000 - now_ms), ats_minutes)


@dataclass(frozen=True)
class HomeFigures:
    boards: int | None
    feeds: int | None
    new_matches: int | None


def home_figures(request: Request) -> HomeFigures:
    """Each figure degrades to None ("—") on its own."""
    from src.settings.boards import board_entries
    from src.web.settings.companies import discovery_only_count
    from src.web.settings.readiness import AGGREGATOR_FAMILIES

    state, cfg = request.app.state, request.state.snapshot.cfg
    boards = feeds = new = None
    try:
        entries = board_entries(cfg)
        extra = discovery_only_count(state.stores, {e.key for e in entries})
        boards = None if extra is None else len(entries) + extra
    except Exception as exc:  # noqa: BLE001
        log.warning("home_boards_unavailable", extra={"error": str(exc)})
    try:
        feeds = sum(1 for name in AGGREGATOR_FAMILIES if getattr(cfg.sources, name).enabled)
    except Exception as exc:  # noqa: BLE001
        log.warning("home_feeds_unavailable", extra={"error": str(exc)})
    try:
        new = state.repo.status_counts().get("new", 0)
    except Exception as exc:  # noqa: BLE001
        log.warning("home_new_matches_unavailable", extra={"error": str(exc)})
    return HomeFigures(boards=boards, feeds=feeds, new_matches=new)


def _status_ctx(request: Request) -> dict:
    cfg = request.state.snapshot.cfg
    return {
        "status": status_line(request.app.state.ops.liveness(),
                              ats_minutes=cfg.schedules.ats_minutes,
                              now_ms=int(time.time() * 1000)),
        "figures": home_figures(request),
    }


def register_home_routes(app: FastAPI) -> None:
    @app.get("/home", response_class=HTMLResponse)
    def home(request: Request):
        from src.web.settings.readiness import check
        snap = request.state.snapshot
        warnings = check(snap.cfg, has_profile=bool(snap.documents.profile),
                         secret_source=request.app.state.service.secret_source)
        return request.app.state.templates.TemplateResponse(
            request, "home.html",
            {"gs": getting_started(request), "warnings": warnings, **_status_ctx(request)},
        )

    @app.get("/home/status", response_class=HTMLResponse)
    def home_status(request: Request):
        return request.app.state.templates.TemplateResponse(
            request, "_home_status.html", _status_ctx(request),
        )

    @app.post("/home/getting-started/hide")
    def hide(request: Request):
        request.app.state.stores.wizard.put(HIDDEN_KEY, True)
        return RedirectResponse("/home", status_code=303)

    @app.post("/home/getting-started/show")
    def show(request: Request):
        request.app.state.stores.wizard.delete(HIDDEN_KEY)
        return RedirectResponse("/home", status_code=303)
