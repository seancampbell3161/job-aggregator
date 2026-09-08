import pytest

pytestmark = pytest.mark.integration
from tests.conftest import WEASYPRINT_UNAVAILABLE

if WEASYPRINT_UNAVAILABLE is not None:  # OSError from cffi, not ImportError
    pytest.skip(f"needs WeasyPrint + native libs ({WEASYPRINT_UNAVAILABLE})",
                allow_module_level=True)

from src.tailor.content import load_content
from src.tailor.models import TailorResult
from src.tailor.render import render_resume, render_with_fallback
from src.tailor.render.registry import get_template, pack_info
from src.tailor.render.settings import BuilderSettings


@pytest.fixture
def content():
    return load_content("resume/content.example.json")


@pytest.fixture
def tailored():
    return TailorResult.fallback()   # fallback still renders static content


def test_render_resume_produces_one_page_pdf(content, tailored):
    result = render_resume(content, tailored)
    assert result.pdf[:4] == b"%PDF"
    assert result.trimmed == []   # example content fits without trimming


def test_render_resume_defaults_to_classic(content, tailored):
    r = render_resume(content, tailored)
    assert r.pdf[:5] == b"%PDF-"
    assert r.template == "classic"
    assert r.fit_warning is None


def test_settings_caps_and_pages_flow_through(content, tailored):
    s = BuilderSettings(max_bullets_per_experience=1, max_bullets_per_project=1, max_pages=1)
    r = render_resume(content, tailored, settings=s)
    assert r.pdf[:5] == b"%PDF-"


def test_a4_page_size_changes_geometry(content, tailored):
    letter = render_resume(content, tailored).pdf
    a4 = render_resume(content, tailored, settings=BuilderSettings(page_size="a4")).pdf
    assert letter != a4  # different page box -> different bytes


def test_render_with_fallback_recovers_from_broken_pack(tmp_path, content, tailored):
    (tmp_path / "template.html.j2").write_text("{% raise_not_a_tag %}")
    broken = pack_info(tmp_path, source="upload")
    r = render_with_fallback(content, tailored, pack=broken, settings=BuilderSettings())
    assert r.template == "classic"
    assert "failed" in (r.fit_warning or "")


def test_headless_builtin_renders_one_page(content, tailored):
    r = render_resume(content, tailored, pack=get_template("headless"))
    assert r.pdf[:5] == b"%PDF-"
    assert r.template == "headless"
