"""The wizard's step rail: where am I, what did each step produce, and which
steps can I go back to."""
import re

from src.web.app import create_app
from tests.auth_helpers import signed_in_client
from tests.settings_helpers import WEB_TEST_SETTINGS, make_service


def _client(tmp_path, monkeypatch, documents=None, secrets=None):
    monkeypatch.setenv("JOB_AGG_SQLITE_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("JOB_AGG_TAILORED_DIR", str(tmp_path / "tailored"))
    app = create_app(service=make_service(WEB_TEST_SETTINGS, documents=documents or {},
                                          secrets=secrets))
    return signed_in_client(app)


def _desktop_rail(html: str) -> str:
    start = html.index('<nav class="wizard-rail"')
    return html[start:html.index("</nav>", start)]


def _compact_rail(html: str) -> str:
    start = html.index('<details class="wizard-rail-compact"')
    return html[start:html.index("</details>", start)]


def test_rail_header_counts_steps(tmp_path, monkeypatch):
    html = _client(tmp_path, monkeypatch).get("/wizard/resume").text
    assert "Step 2 of 6" in _desktop_rail(html)


def test_viewed_step_is_marked_and_not_a_link(tmp_path, monkeypatch):
    rail = _desktop_rail(_client(tmp_path, monkeypatch).get("/wizard/llm").text)
    assert rail.count('aria-current="step"') == 1
    assert 'href="/wizard/llm"' not in rail


def test_skipped_step_is_a_link_with_its_summary(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    client.post("/wizard/llm/skip")
    rail = _desktop_rail(client.get("/wizard/resume").text)
    assert 'href="/wizard/llm"' in rail
    assert "Skipped — keyword matches only" in rail


def test_steps_past_the_resume_step_are_not_links(tmp_path, monkeypatch):
    rail = _desktop_rail(_client(tmp_path, monkeypatch).get("/wizard/llm").text)
    for slug in ("review", "companies", "notifications", "preview"):
        assert f'href="/wizard/{slug}"' not in rail


def test_revisiting_a_done_step_views_it_and_links_the_resume_step(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, documents={"resume_text": "My résumé"})
    client.post("/wizard/llm/skip")          # llm skipped, resume done → resume step = review
    rail = _desktop_rail(client.get("/wizard/resume").text)
    viewed = re.search(r'<li[^>]*aria-current="step"[^>]*>.*?</li>', rail, re.S).group(0)
    assert "Your résumé" in viewed
    assert "Résumé saved" in viewed
    assert 'href="/wizard/review"' in rail   # jump forward again
    assert "Step 2 of 6" in rail


def test_compact_rail_names_the_viewed_step(tmp_path, monkeypatch):
    html = _client(tmp_path, monkeypatch).get("/wizard/resume").text
    compact = _compact_rail(html)
    summary = re.search(r"<summary>(.*?)</summary>", compact, re.S).group(1)
    assert "Step 2 of 6" in summary and "Your résumé" in summary
    assert compact.count("<li") == 6


def test_done_page_rail(tmp_path, monkeypatch):
    html = _client(tmp_path, monkeypatch).get("/wizard/done").text
    assert "All steps finished" in _desktop_rail(html)
    assert 'aria-current="step"' not in html
