from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from src.auth.service import AuthService
from src.auth.throttle import LoginThrottle
from src.settings.service import ConfigService
from src.state import VALID_STATUSES
from src.stores import Stores, build_stores
from src.web.analytics import MatchAnalytics, register_analytics_routes
from src.web.auth import register_auth_routes, register_login_gate
from src.web.board import BoardProvider, register_board_routes
from src.web.coach import CoachProvider, register_coach_routes
from src.web.context import config_ctx
from src.web.cross_origin import register_cross_origin_guard
from src.web.generation_cache import GenerationCache
from src.web.ops import OpsProvider, register_ops_routes
from src.web.repo import TriageRepo

log = logging.getLogger(__name__)

_HERE = Path(__file__).parent

# The page-size choices the triage list offers. A request's page_size is honored
# only if it's one of these — anything else (blank, or a hand-crafted giant
# value) falls back to the app-configured default, so the slice stays bounded.
ALLOWED_PAGE_SIZES = (10, 25, 50)

# Paths reachable before setup: the setup page itself, static assets, the
# HMAC-token tailor deep link and PDF download (they answer with their own
# invalid-link page when nothing is configured), and the login pages — the
# password comes before settings.
SETUP_EXEMPT_PREFIXES = ("/setup", "/static", "/tailor", "/login", "/welcome", "/logout", "/account")


@asynccontextmanager
async def _lifespan(app: FastAPI):
    from src.web.watchdog import start_watchdog
    app.state.watchdog_task = start_watchdog(app)
    try:
        yield
    finally:
        if app.state.watchdog_task is not None:
            app.state.watchdog_task.cancel()


def create_app(
    repo: TriageRepo | None = None,
    *,
    ops: OpsProvider | None = None,
    match_analytics: MatchAnalytics | None = None,
    board: BoardProvider | None = None,
    coach: CoachProvider | None = None,
    stores: Stores | None = None,
    service: ConfigService | None = None,
    auth: AuthService | None = None,
    page_size: int = 10,
) -> FastAPI:
    app = FastAPI(title="Job Triage", lifespan=_lifespan)
    stores = stores if stores is not None else build_stores()
    app.state.stores = stores
    app.state.service = service if service is not None else ConfigService(stores.settings)
    app.state.auth = auth if auth is not None else AuthService(stores.auth)
    app.state.login_throttle = LoginThrottle()
    app.state.cache = GenerationCache()
    app.state.repo = repo if repo is not None else TriageRepo(stores.seen)
    app.state.board = board if board is not None else BoardProvider(app.state.repo)
    app.state.ops = ops if ops is not None else OpsProvider(
        discovered=stores.discovered, seen=stores.seen, health=stores.health,
        events=stores.events,
    )
    app.state.match_analytics = (
        match_analytics if match_analytics is not None else MatchAnalytics(seen=stores.seen)
    )
    from src.web.audit import AuditProvider, register_audit_routes
    app.state.audit = AuditProvider(rejected=stores.rejected, seen=stores.seen)
    app.state.coach_override = coach
    from src.web.builder import EXAMPLE_CONTENT_PATH, BuilderProvider, register_builder_routes

    service_ref, cache = app.state.service, app.state.cache

    def _builder_content():
        # Previews are not tied to one request, so they read the current snapshot.
        snap = service_ref.snapshot()
        content = snap.documents.content() if snap is not None else None
        if content is not None:
            return content
        from src.tailor.content import load_content
        return load_content(EXAMPLE_CONTENT_PATH)

    def _docx_importer():
        snap = service_ref.snapshot()
        if snap is None:
            return None

        def _build(s):
            try:
                from src.tailor.render.docx_import import build_docx_importer
                return build_docx_importer(s.cfg)
            except Exception as exc:  # noqa: BLE001 — Ruling G: a builder must never 500 the request
                log.warning("docx_importer_build_failed", extra={"error": str(exc)})
                return None

        return cache.get("docx_importer", snap, _build)

    app.state.builder = BuilderProvider(
        store=stores.builder, content_loader=_builder_content, importer_source=_docx_importer,
    )
    app.state.tailor_boot_override = None
    app.state.page_size = page_size
    templates = Jinja2Templates(directory=str(_HERE / "templates"))
    from src.web.pipeline_activity import format_ago
    templates.env.filters["ago"] = format_ago
    from src.web.coach import coach_nav_visible
    templates.env.globals["coach_nav_visible"] = coach_nav_visible
    app.state.templates = templates
    app.mount("/static", StaticFiles(directory=str(_HERE / "static")), name="static")
    # Middleware runs in reverse registration order: the cross-origin guard,
    # then the login gate, then the snapshot + setup gate — so requests
    # without a session never read settings.
    _register_setup_gate(app)
    register_login_gate(app)
    register_cross_origin_guard(app)
    _register_routes(app)
    register_auth_routes(app)
    register_ops_routes(app)
    register_analytics_routes(app)
    register_board_routes(app)
    register_audit_routes(app)
    register_coach_routes(app)

    from src.web.kit import register_kit_routes
    register_kit_routes(app)
    register_builder_routes(app)

    from src.web.tailor import register_tailor_routes

    register_tailor_routes(app)

    return app


def _setup_exempt(path: str) -> bool:
    """Whether ``path`` is one of SETUP_EXEMPT_PREFIXES or below one — matched
    at a "/" boundary, so /tailor-history is not exempt just because it starts
    with /tailor."""
    return any(path == p or path.startswith(p + "/") for p in SETUP_EXEMPT_PREFIXES)


def _register_setup_gate(app: FastAPI) -> None:
    """Take the request's settings snapshot — one per request, per the
    snapshot rule — and, until the instance is set up, send everything outside
    SETUP_EXEMPT_PREFIXES to /setup."""

    @app.middleware("http")
    async def _snapshot_and_setup_gate(request: Request, call_next):
        snap = await run_in_threadpool(request.app.state.service.snapshot)
        request.state.snapshot = snap
        if snap is None and not _setup_exempt(request.scope["path"]):
            if request.method in ("GET", "HEAD"):
                return RedirectResponse("/setup", status_code=303)
            return PlainTextResponse("This instance is not set up yet — see /setup.", status_code=409)
        return await call_next(request)

    @app.get("/setup", response_class=HTMLResponse)
    def setup(request: Request):
        if request.state.snapshot is not None:
            return RedirectResponse("/", status_code=303)
        return request.app.state.templates.TemplateResponse(request, "setup.html", {})


def _ctx(request: Request, **extra) -> dict:
    return {**config_ctx(request), **extra}


def _render_list(
    request: Request,
    *,
    q: str = "",
    status: list[str] | None = None,
    min_score: str = "",
    has_gaps: bool = False,
    workplace: list[str] | None = None,
    sort: str = "score",
    page: int = 1,
    page_size: int = 0,
) -> HTMLResponse:
    """Filter/sort/paginate the matches and render the `_list.html` partial.
    Shared by GET /jobs (query params) and POST /bulk-status (form body) so both
    produce an identical list view."""
    statuses = set(status) if status else None
    # Empty selection (every workplace box unchecked) → no constraint, matching how
    # the status filter fails open; a non-empty subset filters to those buckets.
    workplace_set = set(workplace) if workplace else None
    # The inbox's <input type="number"> serializes an empty box as min_score="" —
    # which FastAPI would reject (422) for an int param. Parse it here so a blank
    # field means "no floor".
    try:
        min_score_val = int(min_score) if min_score.strip() else None
    except ValueError:
        min_score_val = None
    matches = request.app.state.repo.list(
        statuses=statuses, min_score=min_score_val, has_gaps=has_gaps,
        workplace=workplace_set, query=q, sort=sort,
    )
    size = page_size if page_size in ALLOWED_PAGE_SIZES else request.app.state.page_size
    total = len(matches)
    total_pages = max(1, -(-total // size))  # ceil div
    page = min(max(page, 1), total_pages)     # clamp into range
    start = (page - 1) * size
    window = matches[start : start + size]
    return request.app.state.templates.TemplateResponse(
        request, "_list.html",
        _ctx(
            request, matches=window, page=page, total_pages=total_pages,
            total=total, page_start=start, page_size=size,
        ),
    )


def _register_routes(app: FastAPI) -> None:
    @app.get("/", response_class=HTMLResponse)
    def inbox(request: Request):
        return request.app.state.templates.TemplateResponse(request, "inbox.html", _ctx(request))

    @app.get("/jobs", response_class=HTMLResponse)
    def jobs(
        request: Request,
        q: str = "",
        status: list[str] = Query(default=[]),
        min_score: str = "",
        has_gaps: bool = False,
        workplace: list[str] = Query(default=[]),
        sort: Literal["score", "newest"] = "score",
        # Filter changes submit the form without a page param, so they naturally
        # reset to page 1; only the pager's Prev/Next carry an explicit page.
        page: int = 1,
        # 0 = unset → the app default; the per-page <select> sends 10/25/50.
        page_size: int = 0,
    ):
        return _render_list(
            request, q=q, status=status, min_score=min_score, has_gaps=has_gaps,
            workplace=workplace, sort=sort, page=page, page_size=page_size,
        )

    @app.get("/jobs/new-count", response_class=HTMLResponse)
    def jobs_new_count(request: Request, since: str = ""):
        """Badge fragment for the triage refresh button: '<span>N new</span>'
        when notified rows landed after `since`, else an empty body. Fail-soft:
        an unparseable watermark or store error renders as 'nothing new' — this
        is polled page chrome, not a data API. The watermark is re-serialized
        through fromisoformat so the JS toISOString 'Z' form compares cleanly
        against the store's '+00:00' first_seen strings."""
        try:
            watermark = datetime.fromisoformat(since.replace("Z", "+00:00")).isoformat()
            n = sum(1 for m in request.app.state.repo.list() if m.first_seen > watermark)
        except Exception:  # noqa: BLE001
            return HTMLResponse("")
        return HTMLResponse(f'<span class="new-badge">{n} new</span>' if n else "")

    @app.post("/bulk-status", response_class=HTMLResponse)
    async def bulk_status(request: Request):
        """Apply one status to many rows at once, then re-render the list in place
        (dismissed rows drop out of the default view). Reads the urlencoded body
        by hand so the route needs no python-multipart dependency — the same
        reason /status takes its params in the query string."""
        form = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)

        def first(key: str, default: str = "") -> str:
            vals = form.get(key)
            return vals[0] if vals else default

        def as_int(key: str, default: int) -> int:
            try:
                return int(first(key, str(default)))
            except ValueError:
                return default

        to_status = first("to_status", "dismissed")
        if to_status not in VALID_STATUSES:
            raise HTTPException(status_code=400, detail=f"invalid status: {to_status}")
        repo = request.app.state.repo
        seen = request.app.state.stores.seen
        for job_id in form.get("ids", []):
            repo.set_status(job_id, to_status)  # missing/expired rows are no-ops
            seen.update_email_suggestion(job_id, suggestion=None)
        return _render_list(
            request, q=first("q"), status=form.get("status", []),
            min_score=first("min_score"), has_gaps=first("has_gaps") == "true",
            workplace=form.get("workplace", []),
            sort=first("sort", "score"), page=as_int("page", 1),
            page_size=as_int("page_size", 0),
        )

    @app.get("/detail", response_class=HTMLResponse)
    def detail(request: Request, id: str):
        m = request.app.state.repo.get(id)
        templates = request.app.state.templates
        if m is None:
            return templates.TemplateResponse(
                request, "_expired.html", _ctx(request), headers={"HX-Trigger": "refreshList"}
            )
        return templates.TemplateResponse(request, "_detail.html", _ctx(request, m=m))

    @app.post("/status", response_class=HTMLResponse)
    def set_status(request: Request, id: str, status: str):
        if status not in VALID_STATUSES:
            raise HTTPException(status_code=400, detail=f"invalid status: {status}")
        repo = request.app.state.repo
        templates = request.app.state.templates
        repo.set_status(id, status)  # False (vanished row) handled by the get below
        request.app.state.stores.seen.update_email_suggestion(id, suggestion=None)
        m = repo.get(id)
        if m is None:
            return templates.TemplateResponse(
                request, "_expired.html", _ctx(request), headers={"HX-Trigger": "refreshList"}
            )
        return templates.TemplateResponse(
            request, "_detail.html", _ctx(request, m=m), headers={"HX-Trigger": "refreshList"}
        )
