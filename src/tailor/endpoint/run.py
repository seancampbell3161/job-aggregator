"""Tailor + render + store core for the endpoint. Dependency-injected (engine,
content, jd_reader, storage) so it is unit-testable without AWS or WeasyPrint.
Never raises out — failures become an {"error": ...} dict the page renders."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable

from src.tailor.endpoint.jd import PostingJD
from src.tailor.endpoint.storage import PdfStorage
from src.tailor.models import ResumeContent, TailorResult, render_input_dict, result_from_render_input

log = logging.getLogger(__name__)


def run_tailor(*, job_id: str, regen: bool, engine: Any, content: ResumeContent,
               jd_reader: Callable[[str], PostingJD | None], storage: PdfStorage,
               url_ttl: int = 900,
               renderer: Callable[[ResumeContent, TailorResult], Any] | None = None,
               template: str = "") -> dict:
    try:
        if template:  # re-render a stored run through a different template — no LLM
            pdf_key = f"{job_id}.{template}.pdf"
            if not regen and storage.exists(pdf_key):
                return {"pdf_url": storage.url(pdf_key, url_ttl), "cached": True, "template": template}
            raw = storage.get(f"{job_id}.result.json")
            if raw is None:
                return {"error": "No stored run to re-render — tailor this job first."}
            result = result_from_render_input(json.loads(raw))
            rendered = _render(renderer, content, result, template)
            storage.put(pdf_key, rendered.pdf, "application/pdf")
            return {"pdf_url": storage.url(pdf_key, url_ttl), "cached": False,
                    "template": rendered.template, "fit_warning": rendered.fit_warning,
                    "trimmed": len(rendered.trimmed)}

        key = f"{job_id}.pdf"
        if not regen and storage.exists(key):
            return {"pdf_url": storage.url(key, url_ttl), "cached": True}

        jd = jd_reader(job_id)
        if jd is None:
            return {"error": "The job description for this alert is no longer stored."}

        if engine is None:
            result = TailorResult.fallback()
        else:
            result = asyncio.run(engine.tailor(job_id=job_id, jd_text=jd.description))

        # Persisted even for a fallback TailorResult: assemble_render_doc falls back to the
        # original bullets, so a fallback run's template re-renders reproduce what the
        # original (untailored) run delivered — that's deliberate, not a bug.
        storage.put(f"{job_id}.result.json",
                    json.dumps(render_input_dict(result)).encode(), "application/json")
        rendered = _render(renderer, content, result)
        storage.put(key, rendered.pdf, "application/pdf")
        return {
            "pdf_url": storage.url(key, url_ttl),
            "cover_letter": result.cover_letter,
            "fit": {"matches": result.fit.matches, "gaps": result.fit.gaps, "overall": result.fit.overall},
            "trimmed": len(rendered.trimmed),
            "fit_warning": rendered.fit_warning,
            "template": rendered.template,
            "cached": False,
        }
    except Exception as exc:  # noqa: BLE001 — the page renders {"error"}; never 500 the handler
        log.warning("tailor_endpoint_run_failed",
                    extra={"job_id": job_id, "error": str(exc), "error_type": type(exc).__name__})
        return {"error": "Couldn't tailor this résumé right now. Please try again."}


def _render(renderer, content, result, template: str = ""):
    if renderer is not None:
        return renderer(content, result)
    from src.tailor.render import render_resume  # lazy: keeps WeasyPrint out of import
    if template:
        from src.tailor.render.registry import get_template
        return render_resume(content, result, pack=get_template(template))
    return render_resume(content, result)
