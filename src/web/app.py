from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException, Query, Request, UploadFile
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

# Liveness for the container healthcheck and the image smoke tests. Exact
# match, not a prefix: it is the one path that must answer 200 in every state,
# so nothing may be mounted beneath it.
HEALTH_PATH = "/healthz"


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
    from src.web.pipeline_activity import format_ago, tier_label
    templates.env.filters["ago"] = format_ago
    templates.env.filters["tier_label"] = tier_label
    from src.web.labels import choice_label, document_label
    templates.env.filters["choice_label"] = choice_label
    templates.env.filters["document_label"] = document_label
    from src.web.coach import coach_nav_visible
    templates.env.globals["coach_nav_visible"] = coach_nav_visible
    from src.web.home import landing_url, register_home_routes
    templates.env.globals["landing_url"] = landing_url
    from src.web.nav import nav_context
    templates.env.globals["nav_context"] = nav_context
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
    register_home_routes(app)

    from src.web.kit import register_kit_routes
    register_kit_routes(app)
    register_builder_routes(app)

    from src.web.settings import register_settings_routes
    register_settings_routes(app)

    from src.web.wizard import register_wizard_routes
    register_wizard_routes(app)

    from src.web.tailor import register_tailor_routes

    register_tailor_routes(app)

    return app


def _setup_exempt(path: str) -> bool:
    """Whether ``path`` is one of SETUP_EXEMPT_PREFIXES or below one — matched
    at a "/" boundary, so /tailor-history is not exempt just because it starts
    with /tailor."""
    return any(path == p or path.startswith(p + "/") for p in SETUP_EXEMPT_PREFIXES)


def _setup_page(request: Request, *, status_code: int = 200, **extra) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request, "setup.html", extra, status_code=status_code,
    )


# restore_from_upload's ImportGuardRefused closing sentence (see
# OVERWRITE_CHECKBOX_HINT in src/web/settings/backup.py) for /setup/restore's
# own page, which — unlike /settings/backup/import — has no overwrite
# checkbox. This branch is reachable here precisely because a settings
# version can come to exist between this request's own idempotence check
# (read once, at the top of setup_restore, before its upload is read and
# extracted) and import_dir's later guard check — see setup_restore's
# docstring below — and whenever it fires, that means a settings version now
# exists, so Settings → Backup (which does have the checkbox) is where to
# finish the job.
_SETUP_RESTORE_GUARD_HINT = (
    "This instance now has settings — reload this page, then restore from "
    "Settings → Backup, which has an overwrite option."
)


def _register_setup_gate(app: FastAPI) -> None:
    """Take the request's settings snapshot — one per request, per the
    snapshot rule — and, until the instance is set up, send everything outside
    SETUP_EXEMPT_PREFIXES to /setup."""

    @app.middleware("http")
    async def _snapshot_and_setup_gate(request: Request, call_next):
        if request.scope["path"] == HEALTH_PATH:
            # Liveness: answer without reading settings, so the probe still
            # works when the settings store is what is broken.
            return await call_next(request)
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
        return _setup_page(request)

    @app.post("/setup/start")
    def setup_start(request: Request):
        """Write a defaults-only settings version so the UI becomes reachable.

        Nothing polls until titles and a source are added — Overview says so.
        Idempotent: if a version already exists (two visitors racing the
        button), this writes nothing."""
        service = request.app.state.service
        if request.state.snapshot is None:
            service.save_settings({}, source="ui", note="started from defaults")
        return RedirectResponse("/settings/filters", status_code=303)

    @app.post("/setup/wizard")
    def setup_wizard(request: Request):
        """Start guided setup.

        Writes the same defaults-only version setup_start writes, with a
        different source, BEFORE the first wizard page — which is what keeps
        /wizard/* out of SETUP_EXEMPT_PREFIXES: by the time the redirect
        lands, the instance is set up. Idempotent, like setup_start."""
        service = request.app.state.service
        if request.state.snapshot is None:
            service.save_settings({}, source="wizard", note="started guided setup")
        return RedirectResponse("/wizard", status_code=303)

    @app.post("/setup/restore")
    async def setup_restore(request: Request, archive: UploadFile | None = None):
        """Restore a backup on a fresh install, with no shell needed.

        Idempotent like setup_start above: if a settings version already
        exists (two visitors racing this form, or someone reloading /setup
        after another tab already restored), nothing is written here —
        this redirects to /settings/backup instead, the configured
        instance's own restore path, which runs the import guard rather than
        silently clobbering whatever is already there. On success, redirects
        to /settings/overview, the page that says what is still missing
        (titles, a source, a notification target) rather than back to /,
        which would 303 right back to /setup until those are added.

        That idempotence check above is read once, at the top of this
        request, before restore_from_upload's own (possibly slow, up to
        MAX_UPLOAD_BYTES) read-and-extract runs — so it does NOT guarantee no
        settings version exists by the time import_dir's own guard is
        evaluated moments later. If a real settings change lands in that
        window (another tab finishes /setup/start, then edits real filters
        or companies, while this upload is still in flight), import_dir can
        still raise ImportGuardRefused here. That is exactly why this call
        passes its own _SETUP_RESTORE_GUARD_HINT rather than
        backup.OVERWRITE_CHECKBOX_HINT: /setup's page has no such checkbox,
        and whenever this guard does fire, a settings version now exists —
        so the hint points at Settings → Backup, which does have one.

        Reuses restore_from_upload (src/web/settings/backup.py) — the same
        bounded-read / extract / import_dir(trust_paths=False) pipeline
        /settings/backup/import uses — rather than a second copy of it, so an
        uploaded config.yaml gets the same LEGACY_PATH_KEYS confinement
        there: an absolute or archive-escaping legacy path is ignored (with a
        warning), never read off this server's disk on the uploader's
        behalf."""
        from src.web.settings.backup import restore_from_upload

        if request.state.snapshot is not None:
            return RedirectResponse("/settings/backup", status_code=303)
        if archive is None or not archive.filename:
            return _setup_page(request, status_code=400,
                                errors=["Choose a file to restore from."])

        outcome = await restore_from_upload(
            request, archive, force=False, guard_hint=_SETUP_RESTORE_GUARD_HINT,
        )
        if not outcome.ok:
            return _setup_page(request, status_code=outcome.status_code, errors=outcome.errors)
        return RedirectResponse("/settings/overview", status_code=303)

    @app.get(HEALTH_PATH, response_class=PlainTextResponse)
    def healthz() -> str:
        return "ok"


def _ctx(request: Request, **extra) -> dict:
    return {**config_ctx(request), **extra}


# The statuses the Matches list shows by default (inbox.html's checked boxes).
_OPEN_STATUSES = ("new", "interested", "applied", "interviewing")


def _empty_reason(request: Request) -> str:
    """Why the list is empty — so a newcomer is told whether to wait, widen
    the search, or loosen the filters, or that everything has been reviewed.
    Falls back to "filtered", the least alarming message, when telemetry
    can't be read."""
    from src.web.home import request_liveness, request_status_counts
    try:
        counts = request_status_counts(request)
        if sum(counts.values()) > 0:
            # Every match has been dealt with: nothing any filter could reveal
            # in the default view, so say so instead of offering a reset. But
            # "caught_up"'s only action is "Show dismissed" — if the request
            # already has that box ticked and is still empty, some other
            # filter (q, min_score, ...) is doing the hiding, so the ordinary
            # "loosen your filters" message applies instead.
            if (not any(counts.get(s, 0) for s in _OPEN_STATUSES)
                    and "dismissed" not in request.query_params.getlist("status")):
                return "caught_up"
            return "filtered"
        live = request_liveness(request)
    except Exception as exc:  # noqa: BLE001 — an empty-state hint never breaks the list
        log.warning("empty_reason_unavailable", extra={"error": str(exc)})
        return "filtered"
    if live is None:        # telemetry unreadable
        return "filtered"
    if live.last_cycle_ms is None:
        return "no_check"
    return "none_yet"


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
    # The inbox's min_score <select> submits "" for its "Any" option — which
    # FastAPI would reject (422) for an int param. Parse it here so a blank
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
            empty_reason=_empty_reason(request) if not window else None,
            ats_minutes=request.state.snapshot.cfg.schedules.ats_minutes,
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
