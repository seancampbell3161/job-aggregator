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


def register_tailor_routes(app: FastAPI) -> None:
    @app.get("/tailor", response_class=HTMLResponse)
    def tailor(request: Request, job_id: str = "", t: str = "", run: str = "",
               regen: str = "", template: str = ""):
        secret = os.environ.get("JOB_AGG_TAILOR_SIGNING_SECRET", "")
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

        boot = getattr(request.app.state, "tailor_boot", None)
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
