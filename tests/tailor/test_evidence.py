import json
import pytest

from src.tailor.evidence import load_evidence


_GOOD = {
    "projects": [
        {"key": "acme-billing", "summary": "Billing rework",
         "metrics": [{"claim": "Cut latency 40%", "ticket_refs": ["JIRA-1234"]}],
         "achievements": [{"text": "Migrated 12 services", "ticket_refs": ["JIRA-1240"]}]},
    ],
}


def _write(tmp_path, obj):
    p = tmp_path / "evidence.json"
    p.write_text(json.dumps(obj))
    return p


def test_load_evidence_happy(tmp_path):
    bank = load_evidence(_write(tmp_path, _GOOD))
    assert bank.projects[0].key == "acme-billing"
    assert bank.ticket_refs() == {"JIRA-1234", "JIRA-1240"}


def test_load_evidence_empty_bank_ok(tmp_path):
    bank = load_evidence(_write(tmp_path, {"projects": []}))
    assert bank.projects == []
    assert bank.ticket_refs() == set()


def test_load_evidence_missing_projects_key_fails_loud(tmp_path):
    with pytest.raises(ValueError, match="missing required key 'projects'"):
        load_evidence(_write(tmp_path, {"foo": 1}))
