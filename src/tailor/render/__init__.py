"""Résumé rendering: content + tailored output + template pack + settings → PDF."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from src.tailor.models import ResumeContent, TailorResult
from src.tailor.render.assemble import apply_bullet_caps, assemble_render_doc, fit_to_pages
from src.tailor.render.pdf import page_count, render_html_to_pdf
from src.tailor.render.registry import DEFAULT_SLUG, TemplateInfo, get_template
from src.tailor.render.settings import BuilderSettings, page_setup_css
from src.tailor.render.template import render_html

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RenderResult:
    pdf: bytes
    trimmed: list[str]                 # bullets dropped (caps + page fitting)
    fit_warning: str | None = None     # set when the doc still overflows max_pages
    template: str = DEFAULT_SLUG       # slug that actually rendered


def render_resume(content: ResumeContent, tailored: TailorResult, *,
                  pack: TemplateInfo | None = None,
                  settings: BuilderSettings | None = None) -> RenderResult:
    """Assemble, cap bullets, fit to max_pages, and render the tailored résumé
    to PDF bytes using the given template pack + settings (both default when
    omitted: the active/classic pack and stock BuilderSettings)."""
    settings = settings or BuilderSettings()
    pack = pack or get_template(settings.active_template)
    doc = assemble_render_doc(content, tailored)
    capped = apply_bullet_caps(
        doc, max_experience=settings.max_bullets_per_experience,
        max_project=settings.max_bullets_per_project,
    )
    css = page_setup_css(settings)
    base_url = str(pack.path)

    def _pages(d) -> int:
        return page_count(render_html_to_pdf(render_html(d, pack), base_url=base_url, extra_css=css))

    fitted, trimmed, warning = fit_to_pages(
        doc, _pages, max_pages=settings.max_pages, min_bullets=settings.min_bullets_per_entry)
    pdf = render_html_to_pdf(render_html(fitted, pack), base_url=base_url, extra_css=css).write_pdf()
    return RenderResult(pdf=pdf, trimmed=capped + trimmed, fit_warning=warning, template=pack.slug)


def render_with_fallback(content: ResumeContent, tailored: TailorResult, *,
                         pack: TemplateInfo, settings: BuilderSettings) -> RenderResult:
    """Render with the chosen pack; if it blows up (bad upload, missing font
    file), fall back to classic so the tapped-for PDF always arrives."""
    try:
        return render_resume(content, tailored, pack=pack, settings=settings)
    except Exception as exc:  # noqa: BLE001 — any template failure falls back
        if pack.slug == DEFAULT_SLUG:
            raise
        log.warning("template_render_failed_falling_back",
                    extra={"slug": pack.slug, "error": str(exc)})
        r = render_resume(content, tailored, pack=get_template(DEFAULT_SLUG), settings=settings)
        note = f"template '{pack.slug}' failed; rendered with classic"
        return RenderResult(pdf=r.pdf, trimmed=r.trimmed, template=r.template,
                            fit_warning=f"{note} — {r.fit_warning}" if r.fit_warning else note)
