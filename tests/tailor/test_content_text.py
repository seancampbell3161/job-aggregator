from pathlib import Path

import pytest

from src.tailor.content import load_content, parse_content
from src.tailor.evidence import load_evidence, parse_evidence

EXAMPLE = Path("resume/content.example.json")


def test_parse_content_matches_load_content():
    assert parse_content(EXAMPLE.read_text()) == load_content(EXAMPLE)


def test_parse_content_rejects_non_object():
    with pytest.raises(ValueError, match="top level"):
        parse_content("[]")


def test_parse_evidence_from_text(tmp_path):
    assert parse_evidence('{"projects": []}').projects == []
    p = tmp_path / "e.json"
    p.write_text('{"projects": [{"key": "K", "summary": "s"}]}')
    assert load_evidence(p).projects[0].key == "K"


def test_parse_evidence_requires_projects():
    with pytest.raises(ValueError, match="projects"):
        parse_evidence("{}")
