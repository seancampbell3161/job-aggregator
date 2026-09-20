"""Title sets are data, so they can be checked — and what they add has to be
something filters.titles will actually match on."""
import pytest

from src.config import AppConfig
from src.web.app import create_app
from src.web.wizard.title_sets import TITLE_SETS
from tests.auth_helpers import signed_in_client
from tests.settings_helpers import WEB_TEST_SETTINGS, make_service


def _client(tmp_path, monkeypatch, documents=None):
    monkeypatch.setenv("JOB_AGG_SQLITE_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("JOB_AGG_TAILORED_DIR", str(tmp_path / "tailored"))
    app = create_app(service=make_service(
        WEB_TEST_SETTINGS, documents=documents or {"resume_text": "ten years of python"}))
    return signed_in_client(app)


@pytest.mark.parametrize("ts", TITLE_SETS, ids=lambda t: t.key)
def test_a_set_is_a_value_the_titles_filter_accepts(ts):
    AppConfig.model_validate({"filters": {"titles": list(ts.titles)}})


@pytest.mark.parametrize("ts", TITLE_SETS, ids=lambda t: t.key)
def test_titles_are_stored_the_way_they_are_compared(ts):
    """Matching is case-insensitive on word boundaries, so a set holding
    "Software Engineer" would match identically but show the user something
    different from every other chip in the box."""
    for title in ts.titles:
        assert title == title.lower().strip()
        assert title, "an empty title would match nothing and look like a bug"


@pytest.mark.parametrize("ts", TITLE_SETS, ids=lambda t: t.key)
def test_a_set_has_no_repeats(ts):
    assert len(set(ts.titles)) == len(ts.titles)


def test_set_keys_are_unique():
    keys = [ts.key for ts in TITLE_SETS]
    assert len(set(keys)) == len(keys)


def test_the_interview_offers_the_sets(tmp_path, monkeypatch):
    r = _client(tmp_path, monkeypatch).get("/wizard/resume")
    assert r.status_code == 200
    for ts in TITLE_SETS:
        assert ts.label in r.text


def test_the_review_step_offers_the_sets_too(tmp_path, monkeypatch):
    """The review step is where a drafted title list gets corrected, and it
    has the same one-title-only trap."""
    r = _client(tmp_path, monkeypatch).get("/wizard/review")
    assert r.status_code == 200
    assert TITLE_SETS[0].label in r.text
