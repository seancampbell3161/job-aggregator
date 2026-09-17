from src.tailor.endpoint.page import loading_page, error_page


def test_loading_page_is_self_contained_and_embeds_run_fetch():
    html = loading_page("greenhouse:stripe:1", "123.sig", "Senior SWE", "Stripe")
    assert "Senior SWE" in html and "Stripe" in html
    assert "spinner" in html.lower()
    assert "run=1" in html               # JS fetches the ?run route
    assert "http://" not in html and "https://" not in html   # no external assets
    assert "<script" in html


def test_loading_page_escapes_company_and_title():
    html = loading_page("j", "t", "<b>x", "A&B")
    assert "<b>x" not in html and "&lt;b&gt;x" in html
    assert "A&B" not in html and "A&amp;B" in html


def test_loading_page_escapes_rendered_result_fields():
    # the JS escapes cover_letter + fit (which can contain '<', e.g. "latency < 500ms")
    html = loading_page("j", "t", "", "")
    assert "const esc =" in html
    assert "esc(d.cover_letter)" in html and "esc((d.fit.matches" in html


def test_error_page_shows_message():
    assert "expired" in error_page("This link has expired.").lower()


def test_loading_page_without_templates_has_no_picker():
    html = loading_page("j1", "tok", "T", "C")
    assert 'id="tpl"' not in html


def test_loading_page_with_templates_renders_picker_with_active_selected():
    html = loading_page("j1", "tok", "T", "C",
                        templates=[("classic", "Classic"), ("headless", "Headless")],
                        active="headless")
    assert 'id="tpl"' in html
    assert '<option value="headless" selected>' in html


def test_loading_page_escapes_the_pdf_url_for_the_href():
    html = loading_page("j", "t", "", "")
    assert "const attr =" in html
    assert "attr(d.pdf_url)" in html
    assert "'+d.pdf_url+'" not in html
