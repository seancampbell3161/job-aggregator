import json

from scripts.build_evidence_bank import build_bank


_ROWS = [
    {"Issue key": "JIRA-1", "Summary": "Add caching layer", "Epic Link": "acme-perf"},
    {"Issue key": "JIRA-2", "Summary": "Tune queries", "Epic Link": "acme-perf"},
    {"Issue key": "JIRA-3", "Summary": "New onboarding flow", "Epic Link": "acme-ux"},
]


def test_build_bank_groups_by_epic():
    bank = build_bank(_ROWS, group_by="Epic Link", key_col="Issue key", summary_col="Summary")
    keys = {p["key"] for p in bank["projects"]}
    assert keys == {"acme-perf", "acme-ux"}
    perf = next(p for p in bank["projects"] if p["key"] == "acme-perf")
    assert {a["text"] for a in perf["achievements"]} == {"Add caching layer", "Tune queries"}
    # every achievement carries its ticket ref; metrics are left for hand-curation
    assert perf["achievements"][0]["ticket_refs"][0].startswith("JIRA-")
    assert perf["metrics"] == []


def test_build_bank_skips_rows_without_group():
    rows = _ROWS + [{"Issue key": "JIRA-9", "Summary": "orphan", "Epic Link": ""}]
    bank = build_bank(rows, group_by="Epic Link", key_col="Issue key", summary_col="Summary")
    all_keys = {p["key"] for p in bank["projects"]}
    assert "" not in all_keys
