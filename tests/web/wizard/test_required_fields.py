"""A step says what it needs BEFORE you submit it.

The review step refuses to save without titles and a posting-age bound, but it
only said so after a failed submit — the user filled the form, pressed Save,
and was sent back with red text. Both facts are knowable on arrival.
"""
import re

from src.web.app import create_app
from tests.auth_helpers import signed_in_client
from tests.settings_helpers import WEB_TEST_SETTINGS, make_service


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("JOB_AGG_SQLITE_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("JOB_AGG_TAILORED_DIR", str(tmp_path / "tailored"))
    app = create_app(service=make_service(WEB_TEST_SETTINGS, documents={}))
    return signed_in_client(app)


def _field_block(html: str, path: str) -> str:
    """The rendered .field wrapper for one config path."""
    m = re.search(
        r'<div class="field[^"]*"[^>]*data-path="%s".*?</div>' % re.escape(path),
        html, re.S)
    assert m, f"no field block rendered for {path}"
    return m.group(0)


def test_the_review_step_marks_its_blocking_fields_required_on_arrival(
        tmp_path, monkeypatch):
    r = _client(tmp_path, monkeypatch).get("/wizard/review")
    assert r.status_code == 200
    for path in ("filters.titles", "filters.max_age_days"):
        assert 'class="field-req"' in _field_block(r.text, path), (
            f"{path} blocks the save but is not marked required")


def test_an_unrequired_field_on_the_same_step_is_not_marked(tmp_path, monkeypatch):
    """Marking everything marks nothing."""
    r = _client(tmp_path, monkeypatch).get("/wizard/review")
    assert 'class="field-req"' not in _field_block(r.text, "filters.comp_floor_usd")


def test_the_posting_age_field_explains_the_consequence_not_a_rule(
        tmp_path, monkeypatch):
    """max_age_days is not inherently required — it is required because
    leaving it unset makes the first run alert on every posting already on
    every board. That reason belongs where the value is chosen."""
    block = _field_block(_client(tmp_path, monkeypatch).get("/wizard/review").text,
                         "filters.max_age_days")
    assert "first run" in block.lower()


def test_required_notes_come_from_the_readiness_codes_the_step_blocks_on():
    """One source of truth: the paths marked required are derived from the
    same STEP_WARNING_CODES the step already reports failures from, so a new
    blocking check cannot be added without its field being marked."""
    from src.web.wizard.routes import STEP_WARNING_CODES, WARNING_PATHS
    for code in STEP_WARNING_CODES["review"]:
        assert code in WARNING_PATHS, (
            f"{code} blocks the review step but names no field to mark")
