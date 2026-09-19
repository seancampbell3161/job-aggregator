"""Uploaded file -> plain text.

Text only: no layout, no styling, no structure. The LLM reads prose, and a
résumé's visual structure carries little the words do not."""
from __future__ import annotations

import io
import logging

log = logging.getLogger(__name__)

# Below this, whatever came out is not a résumé. The common cause by far is a
# PDF that is a scan or an export with no text layer, where extraction yields
# a handful of stray characters — and an empty-ish document handed to the LLM
# produces a confident, entirely invented draft.
MIN_CHARS = 200

_PASTE_HINT = (
    "Paste the text of your résumé into the box below instead."
)


class ExtractionFailed(Exception):
    """Carries a message written for the person who uploaded the file."""


def extract_text(data: bytes, filename: str) -> str:
    name = (filename or "").lower()
    if name.endswith(".pdf"):
        text = _pdf(data)
    elif name.endswith(".docx"):
        text = _docx(data)
    elif name.endswith((".txt", ".md", ".markdown", ".text")):
        text = _plain(data)
    else:
        raise ExtractionFailed(
            "That file type isn't supported — upload a PDF, a .docx, or a "
            f"plain-text file. {_PASTE_HINT}"
        )

    text = text.strip()
    if len(text) < MIN_CHARS:
        raise ExtractionFailed(
            "Almost no text came out of that file. If it's a scan or an image, "
            f"there's nothing to read. {_PASTE_HINT}"
        )
    return text


def _plain(data: bytes) -> str:
    # latin-1 maps every byte 0-255 to a character, so it never raises — a
    # UTF-16 file or other binary blob would otherwise sail through as
    # "successfully decoded" mojibake. NUL bytes are the standard tell for
    # binary content (grep -I and git both use exactly this heuristic), so
    # catch it before decoding rather than trusting decode() to fail.
    if b"\x00" in data:
        raise ExtractionFailed(
            f"That file isn't readable as text. {_PASTE_HINT}"
        )
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return data.decode("latin-1")
        except Exception as exc:  # noqa: BLE001
            raise ExtractionFailed(
                f"That file isn't readable as text. {_PASTE_HINT}"
            ) from exc


def _pdf(data: bytes) -> str:
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    except ExtractionFailed:
        raise
    except Exception as exc:  # noqa: BLE001 — pypdf raises a wide variety
        log.warning("resume_pdf_unreadable", extra={"error": str(exc)})
        raise ExtractionFailed(
            f"That PDF couldn't be read. {_PASTE_HINT}"
        ) from exc


def _docx(data: bytes) -> str:
    try:
        import mammoth
        return mammoth.extract_raw_text(io.BytesIO(data)).value
    except Exception as exc:  # noqa: BLE001
        log.warning("resume_docx_unreadable", extra={"error": str(exc)})
        raise ExtractionFailed(
            f"That .docx couldn't be read. {_PASTE_HINT}"
        ) from exc
