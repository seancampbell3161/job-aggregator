"""Résumé upload: PDF, DOCX, or plain text — and an honest message when the
file is a picture of a résumé rather than a résumé."""
import io

import pytest

from src.resume_intake.extract import MIN_CHARS, ExtractionFailed, extract_text

LONG = ("Platform engineer with ten years of Python and Kubernetes. " * 8)


def _pdf(text: str) -> bytes:
    """A real one-page PDF with a text layer, built with reportlab if
    available, else skipped."""
    try:
        from reportlab.pdfgen import canvas
    except ImportError:
        pytest.skip("reportlab not installed")
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    y = 800
    for line in [text[i:i + 90] for i in range(0, len(text), 90)]:
        c.drawString(40, y, line)
        y -= 14
    c.save()
    return buf.getvalue()


def test_plain_text_passes_through():
    assert "Platform engineer" in extract_text(LONG.encode(), "resume.txt")


def test_markdown_passes_through():
    assert "Platform engineer" in extract_text(LONG.encode(), "resume.md")


def test_docx_is_extracted(tmp_path):
    docx = pytest.importorskip("docx", reason="python-docx not installed")
    from docx import Document
    doc = Document()
    doc.add_paragraph(LONG)
    path = tmp_path / "r.docx"
    doc.save(path)
    assert "Platform engineer" in extract_text(path.read_bytes(), "r.docx")


def test_pdf_is_extracted():
    assert "Platform engineer" in extract_text(_pdf(LONG), "r.pdf")


def test_scanned_pdf_says_so():
    """An image-only PDF extracts to nearly nothing. Handing that to the LLM
    would produce a confidently empty draft, so refuse with a message that
    tells the user what to do instead."""
    from pypdf import PdfWriter
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    buf = io.BytesIO()
    writer.write(buf)
    with pytest.raises(ExtractionFailed) as exc:
        extract_text(buf.getvalue(), "scan.pdf")
    # _PASTE_HINT is appended to every ExtractionFailed message in this
    # module, so asserting on "paste" alone can't tell which code path
    # raised — pin the MIN_CHARS-specific wording instead, since that's the
    # path a scanned/image-only PDF is supposed to take.
    assert "scan or an image" in str(exc.value).lower()


def test_short_text_is_refused():
    with pytest.raises(ExtractionFailed):
        extract_text(b"Bob", "resume.txt")


def test_min_chars_is_the_boundary():
    assert extract_text(("x" * MIN_CHARS).encode(), "r.txt")
    with pytest.raises(ExtractionFailed):
        extract_text(("x" * (MIN_CHARS - 1)).encode(), "r.txt")


def test_unknown_extension_is_refused():
    with pytest.raises(ExtractionFailed) as exc:
        extract_text(LONG.encode(), "resume.pages")
    assert "PDF" in str(exc.value)


def test_corrupt_pdf_is_refused_not_raised_raw():
    with pytest.raises(ExtractionFailed):
        extract_text(b"%PDF-1.4 garbage", "r.pdf")


def test_undecodable_bytes_are_refused():
    with pytest.raises(ExtractionFailed):
        extract_text(b"\xff\xfe\x00\x00" * 50, "r.txt")


def test_utf16_resume_is_extracted():
    """A Notepad "Unicode" save is UTF-16 with a BOM and a NUL byte between
    every ASCII character. That's genuine text, not binary — it must be
    accepted, not caught by the binary/NUL heuristic in test_undecodable_
    bytes_are_refused."""
    assert "Platform engineer" in extract_text(LONG.encode("utf-16"), "resume.txt")


def test_extension_matching_ignores_case():
    assert "Platform engineer" in extract_text(LONG.encode(), "RESUME.TXT")
