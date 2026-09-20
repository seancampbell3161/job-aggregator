"""The controls every wizard step ends with: one primary action, one way to
skip, on one row."""
import re

import pytest

from src.web.app import create_app
from tests.auth_helpers import signed_in_client
from tests.settings_helpers import WEB_TEST_SETTINGS, make_service


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
    assert 'form="wizard-skip"' in r.text, "skip button should join the actions row"
    assert '<div class="actions">' in r.text


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
