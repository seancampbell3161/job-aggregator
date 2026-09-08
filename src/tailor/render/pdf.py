"""WeasyPrint HTML→PDF. base_url resolves the @font-face url('fonts/...')."""

from __future__ import annotations

from typing import Any


class RenderError(RuntimeError):
    """Raised when content cannot be rendered to a single page."""


def render_html_to_pdf(html: str, *, base_url: str, extra_css: str | None = None) -> Any:
    from weasyprint import CSS, HTML
    stylesheets = [CSS(string=extra_css)] if extra_css else None
    return HTML(string=html, base_url=base_url).render(stylesheets=stylesheets)  # a weasyprint Document


def page_count(doc: Any) -> int:
    return len(doc.pages)
