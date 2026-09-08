import pytest

pytestmark = pytest.mark.integration  # excluded from the default run; needs WeasyPrint

from tests.conftest import WEASYPRINT_UNAVAILABLE

if WEASYPRINT_UNAVAILABLE is not None:  # OSError from cffi, not ImportError
    pytest.skip(f"needs WeasyPrint + native libs ({WEASYPRINT_UNAVAILABLE})",
                allow_module_level=True)

import weasyprint  # noqa: E402  — guarded above


def test_render_html_to_pdf_one_page():
    from src.tailor.render.pdf import render_html_to_pdf, page_count
    html = "<html><body><p>hello</p></body></html>"
    doc = render_html_to_pdf(html, base_url="resume")
    assert page_count(doc) == 1
    assert doc.write_pdf()[:4] == b"%PDF"


def test_render_html_to_pdf_extra_css_none_matches_default_geometry():
    from src.tailor.render.pdf import render_html_to_pdf
    html = "<html><body><p>hello</p></body></html>"
    default_page = render_html_to_pdf(html, base_url="resume").pages[0]
    explicit_none_page = render_html_to_pdf(html, base_url="resume", extra_css=None).pages[0]
    assert (default_page.width, default_page.height) == (explicit_none_page.width, explicit_none_page.height)


def test_render_html_to_pdf_extra_css_overrides_page_geometry():
    from src.tailor.render.pdf import render_html_to_pdf
    html = "<html><body><p>hello</p></body></html>"
    # two explicit, differing sizes -> geometry must differ regardless of
    # what WeasyPrint's own default page size happens to be
    letter_page = render_html_to_pdf(html, base_url="resume", extra_css="@page { size: letter }").pages[0]
    a4_page = render_html_to_pdf(html, base_url="resume", extra_css="@page { size: a4 }").pages[0]
    assert (letter_page.width, letter_page.height) != (a4_page.width, a4_page.height)
