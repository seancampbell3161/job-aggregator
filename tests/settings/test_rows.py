"""Rows: digest addressing and the patches that mutate a list-of-model field."""
import pytest

from src.config import AppConfig
from src.settings.errors import SettingsInvalid
from src.settings.rows import (
    NotRowsPath, RowGone, add_row_patch, find_row, list_rows, remove_row_patch,
    row_digest, update_row_patch,
)

TWO = AppConfig.model_validate({
    "sources": {"workday": [
        {"tenant": "microsoft", "region": "wd1", "site": "External"},
        {"tenant": "salesforce", "region": "wd12", "site": "External"},
    ]}
})


def test_rows_carry_their_values():
    rows = list_rows(TWO, "sources.workday")
    assert [r.values["tenant"] for r in rows] == ["microsoft", "salesforce"]


def test_digest_is_stable_across_configs_and_key_order():
    a = row_digest({"tenant": "microsoft", "region": "wd1", "site": "External"})
    b = row_digest({"site": "External", "region": "wd1", "tenant": "microsoft"})
    assert a == b
    assert a == list_rows(TWO, "sources.workday")[0].digest


def test_digest_differs_per_entry():
    rows = list_rows(TWO, "sources.workday")
    assert rows[0].digest != rows[1].digest


def test_find_row_raises_when_the_digest_is_gone():
    with pytest.raises(RowGone):
        find_row(TWO, "sources.workday", "deadbeefcafe")


def test_a_scalar_list_is_rows_of_one_field():
    cfg = AppConfig.model_validate({"sources": {"greenhouse": ["acme", "beta"]}})
    rows = list_rows(cfg, "sources.greenhouse")
    assert [r.values["value"] for r in rows] == ["acme", "beta"]
    assert rows[0].entry == "acme"


def test_a_scalar_row_is_added_and_removed_as_a_bare_string():
    cfg = AppConfig.model_validate({"sources": {"greenhouse": ["acme"]}})
    added = add_row_patch(cfg, "sources.greenhouse", {"value": "beta"})
    assert added["sources.greenhouse"] == ["acme", "beta"]
    digest = list_rows(cfg, "sources.greenhouse")[0].digest
    assert remove_row_patch(cfg, "sources.greenhouse", digest)["sources.greenhouse"] == []


def test_a_blank_scalar_row_is_invalid():
    with pytest.raises(SettingsInvalid):
        add_row_patch(AppConfig(), "sources.greenhouse", {"value": "   "})


def test_a_value_the_element_model_rejects_raises_settings_invalid_on_the_field():
    """The web layer places errors by input name, so the loc must be the
    item-relative field, not a pydantic tuple."""
    with pytest.raises(SettingsInvalid) as exc:
        add_row_patch(AppConfig(), "sources.eightfold",
                      {"slug": "ngc", "domain": "ngc.com", "flavor": "evil"})
    assert [e["loc"] for e in exc.value.errors] == ["flavor"]


def test_a_path_that_is_not_a_list_is_rejected():
    with pytest.raises(NotRowsPath):
        list_rows(TWO, "relevance.score_low")


def test_add_returns_the_whole_new_list():
    patch = add_row_patch(TWO, "sources.workday",
                          {"tenant": "acme", "region": "wd3", "site": "External"})
    assert list(patch) == ["sources.workday"]
    assert [e["tenant"] for e in patch["sources.workday"]] == [
        "microsoft", "salesforce", "acme"]


def test_update_replaces_only_the_addressed_row():
    digest = list_rows(TWO, "sources.workday")[0].digest
    patch = update_row_patch(TWO, "sources.workday", digest,
                             {"tenant": "microsoft", "region": "wd1", "site": "Internal"})
    entries = patch["sources.workday"]
    assert entries[0]["site"] == "Internal"
    assert entries[1]["tenant"] == "salesforce"


def test_update_raises_when_the_row_moved_away_under_us():
    with pytest.raises(RowGone):
        update_row_patch(TWO, "sources.workday", "0123456789ab", {"tenant": "x"})


def test_remove_drops_exactly_one_row():
    digest = list_rows(TWO, "sources.workday")[1].digest
    patch = remove_row_patch(TWO, "sources.workday", digest)
    assert [e["tenant"] for e in patch["sources.workday"]] == ["microsoft"]


def test_remove_of_a_duplicated_entry_drops_only_the_first():
    cfg = AppConfig.model_validate({"sources": {"phenom": [
        {"careers_url": "https://careers.fis.com", "company": "FIS"},
        {"careers_url": "https://careers.fis.com", "company": "FIS"},
    ]}})
    digest = list_rows(cfg, "sources.phenom")[0].digest
    patch = remove_row_patch(cfg, "sources.phenom", digest)
    assert len(patch["sources.phenom"]) == 1


def test_a_union_list_entry_stored_as_a_string_round_trips():
    cfg = AppConfig.model_validate(
        {"sources": {"hiringcafe": {"extra_queries": ["rust", {"query": "go", "location": "DE"}]}}}
    )
    rows = list_rows(cfg, "sources.hiringcafe.extra_queries")
    assert rows[0].values == {"query": "rust", "location": None}
    assert rows[1].values == {"query": "go", "location": "DE"}


def test_a_union_entry_without_a_location_is_written_back_as_a_bare_string():
    cfg = AppConfig.model_validate({"sources": {"hiringcafe": {"extra_queries": ["rust"]}}})
    patch = add_row_patch(cfg, "sources.hiringcafe.extra_queries",
                          {"query": "zig", "location": ""})
    assert patch["sources.hiringcafe.extra_queries"] == ["rust", "zig"]


def test_a_union_entry_with_a_location_is_written_back_as_a_mapping():
    cfg = AppConfig()
    patch = add_row_patch(cfg, "sources.hiringcafe.extra_queries",
                          {"query": "zig", "location": "DE"})
    assert patch["sources.hiringcafe.extra_queries"] == [{"query": "zig", "location": "DE"}]
