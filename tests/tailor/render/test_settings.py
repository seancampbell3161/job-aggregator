import pytest
from pydantic import ValidationError

from src.tailor.render.settings import BuilderSettings, page_setup_css, settings_from_dict


def test_defaults():
    s = BuilderSettings()
    assert s.active_template == "classic"
    assert s.max_bullets_per_experience is None
    assert s.max_bullets_per_project is None
    assert s.min_bullets_per_entry == 1
    assert s.max_pages == 1
    assert s.page_size is None
    assert s.margins is None


def test_rejects_cap_below_min_floor():
    with pytest.raises(ValidationError):
        BuilderSettings(min_bullets_per_entry=3, max_bullets_per_experience=2)
    with pytest.raises(ValidationError):
        BuilderSettings(min_bullets_per_entry=3, max_bullets_per_project=1)


def test_rejects_bad_margins_and_page_size():
    with pytest.raises(ValidationError):
        BuilderSettings(margins="banana")
    with pytest.raises(ValidationError):
        BuilderSettings(margins="0.5in 0.5in 0.5in 0.5in 0.5in")  # 5 tokens
    with pytest.raises(ValidationError):
        BuilderSettings(page_size="legal")


def test_accepts_valid_margin_shorthands():
    for m in ("0.5in", "12pt 14pt", "1cm 2cm 1cm", "0.5in 0.58in 0.5in 0.58in", "15mm"):
        assert BuilderSettings(margins=m).margins == m


def test_settings_from_dict_is_fail_soft():
    assert settings_from_dict(None) == BuilderSettings()
    assert settings_from_dict({}) == BuilderSettings()
    # a bad field falls back to defaults entirely rather than raising
    assert settings_from_dict({"max_pages": "banana"}) == BuilderSettings()
    s = settings_from_dict({"max_pages": 2, "page_size": "a4"})
    assert s.max_pages == 2 and s.page_size == "a4"


def test_page_setup_css():
    # !important: overrides a pack's own hardcoded @page rule (e.g. classic's
    # `size: letter`), which otherwise wins the WeasyPrint cascade (author
    # origin beats the 'user' origin of extra_css at equal specificity).
    assert page_setup_css(BuilderSettings()) is None
    assert page_setup_css(BuilderSettings(page_size="a4")) == "@page { size: a4 !important }"
    assert page_setup_css(BuilderSettings(margins="0.5in")) == "@page { margin: 0.5in !important }"
    assert page_setup_css(BuilderSettings(page_size="letter", margins="1cm 2cm")) == (
        "@page { size: letter !important; margin: 1cm 2cm !important }"
    )
