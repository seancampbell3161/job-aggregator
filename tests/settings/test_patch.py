"""apply_patch: dotted-path edits to a settings document."""
from src.settings.patch import apply_patch


def test_apply_patch_sets_nested_paths_and_reports_change():
    doc = {}
    assert apply_patch(doc, {"filters.comp_floor_usd": 180000}) is True
    assert doc == {"filters": {"comp_floor_usd": 180000}}
    assert apply_patch(doc, {"filters.comp_floor_usd": 180000}) is False


def test_apply_patch_none_removes_the_key_and_prunes_empty_parents():
    doc = {"quiet_hours": {"start": "22:00"}, "filters": {"titles": ["a"]}}
    assert apply_patch(doc, {"quiet_hours": None}) is True
    assert doc == {"filters": {"titles": ["a"]}}


def test_apply_patch_leaves_other_sections_alone():
    doc = {"relevance": {"score_low": 4}}
    apply_patch(doc, {"filters.titles": ["x"]})
    assert doc["relevance"] == {"score_low": 4}
