"""One-time .docx → template pack import: mammoth extracts the structure,
embedded fonts come straight out of the zip, and a single LLM call maps the
placeholders onto the RenderDoc contract. The result is a PENDING pack the
user previews and accepts — the LLM never runs at render time."""

from __future__ import annotations

import io
import logging
import re
import zipfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from src.llm.providers import LlmBinding, build_binding, missing_key
from src.llm.structured import complete_text

log = logging.getLogger(__name__)

RENDERDOC_CONTRACT = """The template is a single Jinja2 HTML file rendered with one variable, `doc`:
- doc.name (str), doc.contact_items (list[str] — phone/email/website/github, pre-ordered)
- doc.skill_rows (list; row.label str, row.tokens list[str])
- doc.experiences (list; e.company, e.role, e.dates strs; e.bullets list[str])
- doc.projects (list; p.name, p.subtitle, p.dates strs; p.bullets list[str])
- doc.education (list; ed.degree, ed.institution, ed.dates strs)
- doc.volunteer (list; v.role, v.org, v.dates strs)
Autoescape is ON. Loop with {% for %}; guard optional sections with {% if %}."""

_RULES = """Rules:
1. Output ONLY the complete HTML file (Jinja2), nothing else.
2. Reproduce the source document's section ORDER, typography and layout as CSS.
3. Include an @page rule (default: size: letter) and self-contained CSS — no external assets.
4. Reference ONLY the provided font files via @font-face with url('fonts/<filename>').
5. Render every doc section the SOURCE document has; omit sections it doesn't have.
6. Use semantic, printable HTML (WeasyPrint renders it)."""


@dataclass(frozen=True)
class DocxExtract:
    html: str
    fonts: list[tuple[str, bytes]]
    hints: dict


def extract_docx(data: bytes) -> DocxExtract:
    import mammoth
    html = mammoth.convert_to_html(io.BytesIO(data)).value
    fonts: list[tuple[str, bytes]] = []
    hints: dict = {}
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for n in z.namelist():
            if n.startswith("word/fonts/") and n.lower().endswith((".ttf", ".otf")):
                fonts.append((Path(n).name, z.read(n)))
        if "word/styles.xml" in z.namelist():
            styles = z.read("word/styles.xml").decode("utf-8", "replace")
            hints["font_names"] = sorted(set(re.findall(r'w:ascii="([^"]+)"', styles)))
            hints["colors"] = sorted(set(re.findall(r'w:color w:val="([0-9A-Fa-f]{6})"', styles)))
    return DocxExtract(html=html, fonts=fonts, hints=hints)


def build_import_prompt(ex: DocxExtract) -> tuple[str, str]:
    """(system, user). The rules and the RenderDoc contract are instructions;
    the converted source document is data out of a file the user uploaded, so
    it goes in the user turn where the provider treats it as content."""
    font_files = ", ".join(name for name, _ in ex.fonts) or "(none — use a websafe stack)"
    system = (
        "Convert a résumé document into a Jinja2 HTML template.\n\n"
        f"{RENDERDOC_CONTRACT}\n\n{_RULES}"
    )
    user = (
        f"Available font files (already in the pack's fonts/ dir): {font_files}\n"
        f"Style hints from the document: {ex.hints}\n\n"
        f"SOURCE DOCUMENT (converted to rough HTML):\n{ex.html}"
    )
    return system, user


def strip_code_fences(text: str) -> str:
    m = re.search(r"```(?:html|jinja2?|j2)?\s*\n(.*?)```", text, re.DOTALL)
    return (m.group(1) if m else text).strip()


_IMPORT_NUM_PREDICT = 16384  # a whole HTML file, same order as a tailoring run


class DocxTemplateImporter:
    """One text completion; raises RuntimeError on failure (the upload route
    turns that into an inline error). The LLM never runs at render time."""

    def __init__(self, *, binding: LlmBinding, timeout_seconds: int) -> None:
        self._binding = binding
        self._timeout = timeout_seconds

    async def to_template(self, data: bytes) -> tuple[str, DocxExtract]:
        ex = extract_docx(data)
        system, user = build_import_prompt(ex)
        try:
            raw = await complete_text(
                self._binding, system=system, user=user,
                max_output_tokens=_IMPORT_NUM_PREDICT,
            )
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"import LLM call failed: {type(exc).__name__}: {exc}") from exc
        text = strip_code_fences(raw or "")
        if "doc." not in text or "<html" not in text.lower():
            raise RuntimeError("import LLM returned something that isn't a doc-driven HTML template")
        return text, ex


def build_docx_importer(cfg: Any) -> DocxTemplateImporter | None:
    """None when the tailoring feature has no usable provider binding — the
    /builder page then shows docx import as unavailable."""
    t = cfg.tailoring
    if missing_key(cfg, "tailoring") is not None:
        return None
    binding = build_binding(cfg, feature="tailoring", timeout_seconds=t.timeout_seconds)
    if binding is None:
        return None
    # Template generation is a longer call than a normal tailoring run; never
    # let a tight tailoring.timeout_seconds starve it.
    timeout = max(binding.timeout_seconds, 120)
    return DocxTemplateImporter(
        binding=replace(binding, timeout_seconds=timeout), timeout_seconds=timeout,
    )
