"""The tailor deep-link routes. The signed token is their only gate — the
login gate leaves them public, so a phone can open an alert's link without a
session: a loading page, a run step, and the resulting PDF served back
through /tailor/pdf with the same token."""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from src.tailor.endpoint.auth import verify_token
from src.tailor.endpoint.page import error_page, loading_page
from src.tailor.endpoint.run import run_tailor
from src.tailor.endpoint.storage import LocalFileStorage, pdf_key
from src.tailor.render.registry import SLUG_RE, get_template, list_templates
from src.tailor.render.settings import settings_from_dict

log = logging.getLogger(__name__)

INVALID_LINK = "This link has expired or is invalid."


def tailored_dir() -> str:
    return os.environ.get("JOB_AGG_TAILORED_DIR", "tailored")


def pdf_url(job_id: str, token: str, template: str = "") -> str:
    """The token-checked download URL for a stored PDF (see /tailor/pdf)."""
    url = f"/tailor/pdf?job_id={quote(job_id, safe='')}&t={quote(token, safe='')}"
    return f"{url}&template={quote(template, safe='')}" if template else url


def download_name(job_id: str) -> str:
    return "resume-" + re.sub(r"[^A-Za-z0-9._-]", "-", job_id) + ".pdf"


def tailor_boot(request: Request):
    """(engine, content) for tailoring runs: the override set on app.state
    (tests), else built from the request's snapshot and cached per settings
    generation. None when not set up, tailoring is disabled, or no résumé
    content document exists. The engine itself may be None (the configured
    provider has no API key, or building it raised — an evidence bank is not
    required for one): stored results can still be re-rendered."""
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


def _signing_secret(request: Request) -> str:
    snap = request.state.snapshot
    return snap.cfg.secrets.tailor_signing_secret if snap is not None else ""


def register_tailor_routes(app: FastAPI) -> None:
    @app.get("/tailor", response_class=HTMLResponse)
    def tailor(request: Request, job_id: str = "", t: str = "", run: str = "",
               regen: str = "", template: str = ""):
        secret = _signing_secret(request)
        if not job_id or not secret or not verify_token(t, job_id, secret):
            return HTMLResponse(error_page(INVALID_LINK))

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
        storage = LocalFileStorage(root=tailored_dir())
        requested = template or settings.active_template
        pack = get_template(requested)
        key_template = pack.slug if template else ""

        def renderer(c, r):
            from src.tailor.render import render_with_fallback
            return render_with_fallback(c, r, pack=pack, settings=settings)

        out = run_tailor(job_id=job_id, regen=(regen == "1"), engine=engine,
                         content=content, jd_reader=store.get_jd, storage=storage,
                         renderer=renderer, template=key_template)
        if out.pop("pdf_key", None) is not None:
            out["pdf_url"] = pdf_url(job_id, t, key_template)
        if "error" not in out and pack.slug != requested:
            warning = f"template '{requested}' is missing — rendered with {pack.slug}"
            existing = out.get("fit_warning")
            out["fit_warning"] = f"{existing} — {warning}" if existing else warning
        return JSONResponse(out)

    @app.get("/tailor/pdf")
    def tailor_pdf(request: Request, job_id: str = "", t: str = "", template: str = ""):
        """A stored tailored PDF, authorized by the deep link's own token, so
        the phone that opened /tailor downloads it without a session. Only
        that job's PDFs are reachable: the file name is rebuilt with
        pdf_key(), never taken from the request."""
        secret = _signing_secret(request)
        if not job_id or not secret or not verify_token(t, job_id, secret):
            return HTMLResponse(error_page(INVALID_LINK), status_code=403)
        if template and not SLUG_RE.match(template):
            return HTMLResponse(error_page("Unknown résumé template."), status_code=400)
        root = Path(tailored_dir()).resolve()
        path = (root / pdf_key(job_id, template)).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            return HTMLResponse(error_page(
                "This PDF isn't stored anymore — open the tailor link again to regenerate it."
            ), status_code=404)
        return FileResponse(
            path, media_type="application/pdf", filename=download_name(job_id),
            content_disposition_type="inline",
            headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
        )
