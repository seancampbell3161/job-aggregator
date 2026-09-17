"""The tailor deep-link routes. Signed-token gate and loading/run two-step,
with PDFs written to a local directory and served by this app."""

from __future__ import annotations

import logging
import os

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from src.tailor.endpoint.auth import verify_token
from src.tailor.endpoint.page import error_page, loading_page
from src.tailor.endpoint.run import run_tailor
from src.tailor.endpoint.storage import LocalFileStorage
from src.tailor.render.registry import get_template, list_templates
from src.tailor.render.settings import settings_from_dict

log = logging.getLogger(__name__)


def tailor_boot(request: Request):
    """(engine, content) for tailoring runs: the override set on app.state
    (tests), else built from the request's snapshot and cached per settings
    generation. None when not set up, tailoring is disabled, or no résumé
    content document exists. The engine itself may be None (no key/evidence):
    stored results can still be re-rendered."""
    override = request.app.state.tailor_boot_override
    if override is not None:
        return override
    snap = request.state.snapshot
    if snap is None:
        return None
    return request.app.state.cache.get("tailor_boot", snap, _build_tailor_boot)


def _build_tailor_boot(snap):
    if not snap.cfg.tailoring.enabled:
        return None
    content = snap.documents.content()
    if content is None:
        return None
    try:
        from src.tailor import build_tailor_engine
        engine = build_tailor_engine(snap.cfg, content, snap.documents.evidence_bank())
    except Exception as exc:  # noqa: BLE001 — Ruling G: a builder must never 500 the request
        log.warning("tailor_engine_build_failed", extra={"error": str(exc)})
        engine = None
    return engine, content


def register_tailor_routes(app: FastAPI) -> None:
    @app.get("/tailor", response_class=HTMLResponse)
    def tailor(request: Request, job_id: str = "", t: str = "", run: str = "",
               regen: str = "", template: str = ""):
        snap = request.state.snapshot
        secret = snap.cfg.secrets.tailor_signing_secret if snap is not None else ""
        if not job_id or not secret or not verify_token(t, job_id, secret):
            return HTMLResponse(error_page("This link has expired or is invalid."))

        store = request.app.state.stores.seen
        settings = settings_from_dict(request.app.state.stores.builder.get())

        if run != "1":
            jd = store.get_jd(job_id)
            return HTMLResponse(loading_page(
                job_id, t, jd.title if jd else "", jd.company if jd else "",
                templates=[(x.slug, x.name) for x in list_templates()],
                active=settings.active_template,
            ))

        boot = tailor_boot(request)
        if boot is None:
            return JSONResponse({"error": "Tailoring is not enabled on this server."})
        engine, content = boot
        storage = LocalFileStorage(
            root=os.environ.get("JOB_AGG_TAILORED_DIR", "tailored"), base_url="/tailored"
        )
        requested = template or settings.active_template
        pack = get_template(requested)

        def renderer(c, r):
            from src.tailor.render import render_with_fallback
            return render_with_fallback(c, r, pack=pack, settings=settings)

        out = run_tailor(job_id=job_id, regen=(regen == "1"), engine=engine,
                         content=content, jd_reader=store.get_jd, storage=storage,
                         renderer=renderer, template=(pack.slug if template else ""))
        if "error" not in out and pack.slug != requested:
            warning = f"template '{requested}' is missing — rendered with {pack.slug}"
            existing = out.get("fit_warning")
            out["fit_warning"] = f"{existing} — {warning}" if existing else warning
        return JSONResponse(out)
