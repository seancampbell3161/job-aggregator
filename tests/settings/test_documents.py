from pathlib import Path

import pytest

from src.settings.documents import DOCUMENT_KINDS, Documents, validate_document
from src.settings.errors import SettingsInvalid

CONTENT = Path("resume/content.example.json").read_text()
FACTS = Path("resume/facts.example.yaml").read_text()


def test_document_kinds():
    assert DOCUMENT_KINDS == ("profile", "resume_text", "resume_content", "evidence", "kit_facts")


@pytest.mark.parametrize("kind,body", [
    ("profile", "# me"),
    ("resume_text", "my résumé"),
    ("resume_content", CONTENT),
    ("evidence", '{"projects": []}'),
    ("kit_facts", FACTS),
])
def test_valid_bodies_pass(kind, body):
    validate_document(kind, body)


@pytest.mark.parametrize("kind,body,fragment", [
    ("profile", "   \n", "must not be empty"),
    ("resume_text", "", "must not be empty"),
    ("resume_content", "{not json", "resume_content"),
    ("resume_content", '{"name": "x"}', "missing required key"),
    ("evidence", "{}", "projects"),
    ("evidence", "[]", "evidence"),
    ("kit_facts", "group: not-a-list\n", "top level must be a list"),
])
def test_invalid_bodies_raise_with_kind_as_loc(kind, body, fragment):
    with pytest.raises(SettingsInvalid) as exc:
        validate_document(kind, body)
    assert fragment in str(exc.value)
    assert exc.value.errors[0]["loc"] == kind


def test_unknown_kind_is_rejected():
    with pytest.raises(SettingsInvalid, match="unknown document kind"):
        validate_document("cover_letter", "x")


def test_documents_parse_helpers():
    docs = Documents(resume_content=CONTENT, evidence='{"projects": []}')
    assert docs.content().name
    assert docs.evidence_bank().projects == []
    assert Documents().content() is None
    assert Documents().evidence_bank() is None


def test_documents_parse_helpers_fail_soft(caplog):
    docs = Documents(resume_content="{broken", evidence="[]")
    with caplog.at_level("WARNING", logger="src.settings.documents"):
        assert docs.content() is None
        assert docs.evidence_bank() is None
    assert [r.message for r in caplog.records].count("document_unparseable") == 2
