"""undone_changes: does importing a file undo settings saved since the last import?"""
from src.settings.import_guard import undone_changes


# -- with a baseline (the last import's settings document) ---------------------------

def test_nothing_changed_since_the_import_allows_any_file():
    base = {"schedules": {"slow_minutes": 30}}
    assert undone_changes(base, base, {"schedules": {"slow_minutes": 90}}) == []


def test_a_stale_scalar_is_reported():
    base = {"schedules": {"slow_minutes": 30}}
    current = {"schedules": {"slow_minutes": 60}}
    assert undone_changes(base, current, base) == [
        "schedules.slow_minutes: would change 60 back to 30"
    ]


def test_a_stale_scalar_that_was_a_default_is_reported():
    current = {"schedules": {"slow_minutes": 60}}
    assert undone_changes({}, current, {}) == [
        "schedules.slow_minutes: would change 60 back to the default"
    ]


def test_a_scalar_reset_to_its_default_since_the_import_is_reported():
    base = {"schedules": {"slow_minutes": 30}}
    assert undone_changes(base, {}, base) == [
        "schedules.slow_minutes: would change the default back to 30"
    ]


def test_a_new_value_for_a_changed_scalar_is_a_deliberate_edit():
    base = {"schedules": {"slow_minutes": 30}}
    current = {"schedules": {"slow_minutes": 60}}
    assert undone_changes(base, current, {"schedules": {"slow_minutes": 45}}) == []


def test_a_merged_file_is_allowed():
    base = {"schedules": {"slow_minutes": 30}}
    current = {"schedules": {"slow_minutes": 60}, "sources": {"greenhouse": ["stripe"]}}
    assert undone_changes(base, current, current) == []


def test_list_entries_added_since_the_import_must_be_in_the_file():
    base = {"sources": {"greenhouse": ["acme"]}}
    current = {"sources": {"greenhouse": ["acme", "stripe", "figma"]}}
    incoming = {"sources": {"greenhouse": ["acme", "figma", "linear"]}}
    assert undone_changes(base, current, incoming) == [
        "sources.greenhouse: would drop stripe"
    ]


def test_a_list_family_created_since_the_import_counts():
    current = {"sources": {"lever": ["acme"]}}
    assert undone_changes({}, current, {}) == ["sources.lever: would drop acme"]


def test_list_entries_removed_since_the_import_must_not_come_back():
    base = {"sources": {"greenhouse": ["acme", "stripe"]}}
    current = {"sources": {"greenhouse": ["acme"]}}
    assert undone_changes(base, current, base) == [
        "sources.greenhouse: would bring back stripe"
    ]


def test_a_reordered_list_is_not_a_change():
    base = {"sources": {"greenhouse": ["acme", "stripe"]}}
    current = {"sources": {"greenhouse": ["stripe", "acme"]}}
    assert undone_changes(base, current, base) == []


def test_structured_list_entries_compare_by_value():
    board = {"tenant": "acme", "region": "wd5", "site": "External"}
    current = {"sources": {"workday": [board]}}
    lines = undone_changes({}, current, {})
    assert lines == ['sources.workday: would drop {"region": "wd5", "site": "External", "tenant": "acme"}']
    assert undone_changes({}, current, {"sources": {"workday": [dict(board)]}}) == []


def test_long_lists_of_lost_entries_are_summarized():
    current = {"sources": {"greenhouse": [f"co{i}" for i in range(8)]}}
    assert undone_changes({}, current, {}) == [
        "sources.greenhouse: would drop co0, co1, co2, co3, co4 and 3 more"
    ]


def test_settings_only_in_the_file_are_new_edits():
    base = {"schedules": {"slow_minutes": 30}}
    assert undone_changes(base, base, {"filters": {"titles": ["engineer"]}}) == []


def test_every_undone_setting_is_reported_in_document_order():
    base = {"schedules": {"slow_minutes": 30}}
    current = {"schedules": {"slow_minutes": 60, "ats_minutes": 3},
               "sources": {"greenhouse": ["stripe"]}}
    assert undone_changes(base, current, base) == [
        "schedules.slow_minutes: would change 60 back to 30",
        "schedules.ats_minutes: would change 3 back to the default",
        "sources.greenhouse: would drop stripe",
    ]


# -- without a baseline (settings were never imported) -----------------------------------

def test_without_a_baseline_every_value_in_effect_must_match():
    current = {"schedules": {"slow_minutes": 20}, "sources": {"greenhouse": ["stripe"]}}
    assert undone_changes(None, current, {"schedules": {"slow_minutes": 30}}) == [
        "schedules.slow_minutes: would change 20 to 30",
        "sources.greenhouse: would drop stripe",
    ]


def test_without_a_baseline_a_file_that_keeps_every_value_is_allowed():
    current = {"schedules": {"slow_minutes": 20}, "sources": {"greenhouse": ["stripe"]}}
    incoming = {"schedules": {"slow_minutes": 20, "ats_minutes": 5},
                "sources": {"greenhouse": ["stripe", "figma"]}}
    assert undone_changes(None, current, incoming) == []


def test_without_a_baseline_dropping_a_value_to_its_default_is_reported():
    assert undone_changes(None, {"schedules": {"slow_minutes": 20}}, {}) == [
        "schedules.slow_minutes: would change 20 to the default"
    ]
