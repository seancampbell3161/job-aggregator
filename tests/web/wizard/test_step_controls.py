"""The controls every wizard step ends with: one primary action, one way to
skip, on one row."""
import re

import pytest

from src.web.app import create_app
from tests.auth_helpers import signed_in_client
from tests.settings_helpers import WEB_TEST_SETTINGS, make_service
from tests.web.wizard.test_review_step import DRAFT, _drafts


def _client(tmp_path, monkeypatch, documents=None):
    monkeypatch.setenv("JOB_AGG_SQLITE_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("JOB_AGG_TAILORED_DIR", str(tmp_path / "tailored"))
    app = create_app(service=make_service(WEB_TEST_SETTINGS, documents=documents or {}))
    return signed_in_client(app)


def _skip_controls(html: str, slug: str) -> int:
    """How many submit controls post to this step's skip endpoint — counting
    both a <form action=...> of its own and a button borrowing one by id."""
    forms = re.findall(r'<form[^>]*action="/wizard/%s/skip"' % slug, html)
    return len(forms)


def _bar(html: str) -> str:
    """The action bar's markup. No <div> appears inside .action-bar-end, so
    the first </div> after it closes the end group and the next the bar."""
    m = re.search(r'<div class="action-bar">.*?<div class="action-bar-end">.*?</div>\s*</div>',
                  html, re.S)
    assert m, "no action bar on the page"
    return m.group(0)


@pytest.mark.parametrize("slug", ["companies", "preview"])
def test_a_step_that_skips_via_its_own_button_gets_no_second_skip(
        tmp_path, monkeypatch, slug):
    """Incomplete, these two steps post Continue/Finish straight at their own
    skip endpoint — /wizard would route right back otherwise. The shared
    "Skip this step" form then posts to the identical URL, so the page
    offered two differently-labelled buttons doing exactly the same thing,
    stacked one above the other."""
    r = _client(tmp_path, monkeypatch).get(f"/wizard/{slug}")
    assert r.status_code == 200
    assert _skip_controls(r.text, slug) == 1


@pytest.mark.parametrize("slug", ["llm", "resume", "notifications"])
def test_a_saving_step_keeps_its_skip_alongside_the_primary_action(
        tmp_path, monkeypatch, slug):
    """Where the primary button really saves, skipping is a distinct choice
    and must stay — but on the same row, not stacked underneath."""
    r = _client(tmp_path, monkeypatch).get(f"/wizard/{slug}")
    assert r.status_code == 200
    assert 'form="wizard-skip"' in r.text, "skip button should join the action bar"
    bar = _bar(r.text)
    assert 'form="wizard-skip"' in bar, "skip button should sit in the action bar"
    assert ">Skip for now<" in bar
    assert ">Save and continue<" in bar


def test_the_companies_step_says_what_is_already_being_polled(tmp_path, monkeypatch):
    """Three aggregator feeds ship enabled, so "no companies" is not "nothing
    polled" — but the step said only "The job boards to poll", which reads as
    though continuing past it leaves you with none. Name what is already on."""
    r = _client(tmp_path, monkeypatch).get("/wizard/companies")
    assert r.status_code == 200
    body = r.text.lower()
    assert "already" in body
    for feed in ("hacker news", "remotive", "remoteok"):
        assert feed in body, f"{feed} is polling by default but is not mentioned"


def test_the_companies_step_does_not_claim_feeds_that_are_switched_off(
        tmp_path, monkeypatch):
    """The list is read from config, not hardcoded — an install that turned
    the aggregators off must not be told they are running."""
    monkeypatch.setenv("JOB_AGG_SQLITE_PATH", str(tmp_path / "off.db"))
    monkeypatch.setenv("JOB_AGG_TAILORED_DIR", str(tmp_path / "tailored"))
    off = {**WEB_TEST_SETTINGS, "sources": {
        "hn_who_is_hiring": {"enabled": False},
        "remotive": {"enabled": False},
        "remoteok": {"enabled": False}}}
    app = create_app(service=make_service(off))
    r = signed_in_client(app).get("/wizard/companies")
    assert "remotive" not in r.text.lower()


def test_first_step_has_no_back(tmp_path, monkeypatch):
    bar = _bar(_client(tmp_path, monkeypatch).get("/wizard/llm").text)
    assert ">Back<" not in bar


def test_later_steps_go_back_one_step(tmp_path, monkeypatch):
    bar = _bar(_client(tmp_path, monkeypatch).get("/wizard/notifications").text)
    assert '<a class="btn ghost" href="/wizard/companies">Back</a>' in bar


@pytest.mark.parametrize("slug,label,primary", [
    ("companies", "Continue", True),
    ("preview", "Finish", False),
])
def test_own_skip_steps_put_their_primary_in_the_bar(tmp_path, monkeypatch, slug, label, primary):
    """Preview, incomplete, is the odd one out: Finish there is really a skip
    ("Run the preview" above is the page's one true primary action), so it
    renders as a secondary button, not a second primary."""
    r = _client(tmp_path, monkeypatch).get(f"/wizard/{slug}")
    bar = _bar(r.text)
    cls = "btn primary" if primary else "btn"
    assert f'class="{cls}">{label}<' in bar
    assert "Skip for now" not in bar
    if not primary:
        assert r.text.count("btn primary") == 1


def test_draft_again_sits_in_the_review_bar(tmp_path, monkeypatch):
    _drafts(monkeypatch, DRAFT)
    bar = _bar(_client(tmp_path, monkeypatch).get("/wizard/review").text)
    assert "Draft again" in bar
    assert bar.index("Draft again") < bar.index("Skip for now") < bar.index("Save and continue")


def test_done_page_has_no_action_bar(tmp_path, monkeypatch):
    assert 'class="action-bar"' not in _client(tmp_path, monkeypatch).get("/wizard/done").text
