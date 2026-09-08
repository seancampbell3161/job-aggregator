"""Acceptance gate for template packs: parses in the sandbox, renders the
user's real content, and produces a PDF. Anything that fails here never
becomes a usable pack."""

from __future__ import annotations

from pathlib import Path

from src.tailor.models import ResumeContent, TailorResult
from src.tailor.render.registry import TEMPLATE_FILENAME, pack_info


def validate_pack(pack_dir: Path, content: ResumeContent) -> str | None:
    if not (pack_dir / TEMPLATE_FILENAME).exists():
        return f"pack is missing {TEMPLATE_FILENAME}"
    try:
        from src.tailor.render import render_resume
        render_resume(content, TailorResult.fallback(), pack=pack_info(pack_dir, source="upload"))
    except Exception as exc:  # noqa: BLE001 — the message is the product here
        return f"template failed to render: {type(exc).__name__}: {exc}"
    return None
