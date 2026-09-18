"""The AppConfig walker that both the settings UI and the docs guard read."""
import pytest

from src.settings.fields import (
    KIND_BOOL, KIND_CHIPS, KIND_CHOICE, KIND_INT, KIND_MULTI_CHOICE,
    KIND_READ_ONLY, KIND_ROWS, KIND_TEXT, KIND_TIME,
    all_paths, editable_fields, field_map, item_fields, item_model, optional_groups,
    rows_paths,
)
from src.config import HiringCafeSearch, WorkdayTenant


def test_scalar_kinds_and_bounds():
    m = field_map()
    assert m["discovery.enabled"].kind == KIND_BOOL
    assert m["discovery.enabled"].default is False

    team = m["discovery.yc_oss_min_team_size"]
    assert team.kind == KIND_INT and team.default == 10
    assert team.ge == 0 and team.le is None

    score = m["relevance.score_high"]
    assert score.ge == 0 and score.le == 10


def test_literal_becomes_a_choice():
    f = field_map()["relevance.provider"]
    assert f.kind == KIND_CHOICE
    assert f.choices == ("anthropic", "gemini", "ollama")


def test_list_of_literal_becomes_multi_choice():
    f = field_map()["filters.seniority_allow"]
    assert f.kind == KIND_MULTI_CHOICE
    assert f.choices == ("junior", "mid", "senior", "staff")
    assert f.default == ("mid", "senior")


def test_list_of_str_becomes_chips():
    assert field_map()["sources.greenhouse"].kind == KIND_CHIPS


def test_list_defaults_are_immutable_tuples():
    """Sequence defaults must be tuples to prevent shared mutable state in cache."""
    m = field_map()
    # multi_choice fields
    assert isinstance(m["filters.seniority_allow"].default, tuple)
    # chips fields
    assert isinstance(m["sources.greenhouse"].default, tuple)
    # verify the specific values are preserved
    assert m["filters.seniority_allow"].default == ("mid", "senior")
    assert m["sources.greenhouse"].default == ()


def test_list_of_model_is_rows():
    assert field_map()["sources.workday"].kind == KIND_ROWS


def test_mixed_str_or_model_list_is_rows():
    assert field_map()["sources.hiringcafe.extra_queries"].kind == KIND_ROWS


def test_every_structured_source_family_is_rows():
    families = ("workday", "oraclecloud", "eightfold", "jsonld_boards",
                "phenom", "taleo", "avature")
    for family in families:
        assert field_map()[f"sources.{family}"].kind == KIND_ROWS, family


def test_slug_families_are_still_chips():
    assert field_map()["sources.greenhouse"].kind != KIND_ROWS


def test_nothing_is_read_only_any_more():
    """KIND_READ_ONLY survives as a concept for a shape nothing handles;
    AppConfig has no such field today. If this fails, a new field was added
    that the row editor cannot render — decide deliberately, don't delete it."""
    assert [f.path for f in field_map().values() if f.kind == KIND_READ_ONLY] == []


def test_item_model_is_the_element_type():
    assert item_model("sources.workday") is WorkdayTenant
    assert item_model("sources.hiringcafe.extra_queries") is HiringCafeSearch
    assert item_model("sources.greenhouse") is None


def test_item_fields_are_item_relative():
    specs = item_fields("sources.workday")
    assert [s.path for s in specs] == ["tenant", "region", "site"]
    assert all(s.kind == KIND_TEXT for s in specs)


def test_item_fields_carry_optionality_and_choices():
    eightfold = {s.path: s for s in item_fields("sources.eightfold")}
    assert eightfold["company"].optional is True
    assert eightfold["flavor"].kind == KIND_CHOICE
    assert eightfold["flavor"].choices == ("pcsx", "apply_v2")
    assert eightfold["flavor"].default == "pcsx"


def test_rows_paths_lists_every_rows_field():
    paths = rows_paths()
    assert "sources.taleo" in paths
    assert "sources.hiringcafe.extra_queries" in paths
    assert "sources.lever" not in paths


def test_a_chips_path_has_one_synthetic_item_field():
    """A slug family is a list of one-field rows, so the Companies page can
    remove a Greenhouse slug through the same editor as a Workday tenant."""
    specs = item_fields("sources.greenhouse")
    assert [s.path for s in specs] == ["value"]
    assert specs[0].kind == KIND_TEXT


def test_item_fields_refuses_a_path_that_is_neither():
    with pytest.raises(KeyError):
        item_fields("relevance.score_low")


def test_optional_scalar_is_marked_optional():
    f = field_map()["filters.max_age_days"]
    assert f.kind == KIND_INT and f.optional is True and f.default is None


def test_optional_model_group_is_descended_and_reported():
    m = field_map()
    assert "quiet_hours" in optional_groups()
    assert m["quiet_hours.start"].kind == KIND_TIME
    assert m["quiet_hours.timezone"].kind == KIND_TEXT


def test_secrets_and_deprecated_keys_are_walked_but_not_editable():
    m = field_map()
    # present, so the docs guard still sees them
    assert "secrets.ntfy_topic_url" in m
    assert "filters.location.remote_must_be_us" in m
    # but never rendered as a settings input
    editable = {f.path for f in editable_fields()}
    assert "secrets.ntfy_topic_url" not in editable
    assert "filters.location.remote_must_be_us" not in editable


def test_all_paths_matches_field_map():
    assert all_paths() == frozenset(field_map())
    # canary: the ForwardRef'd location gate must have been descended into
    assert "filters.location.allowed_countries" in all_paths()


def test_label_is_derived_from_the_leaf_name():
    assert field_map()["discovery.yc_oss_min_team_size"].label == "Yc oss min team size"
    assert field_map()["discovery.enabled"].root == "discovery"


def test_value_at_reads_a_dotted_path_off_a_config():
    from src.config import AppConfig
    from src.settings.fields import value_at
    cfg = AppConfig.model_validate({"filters": {"comp_floor_usd": 180000}})
    assert value_at(cfg, "filters.comp_floor_usd") == 180000
    assert value_at(cfg, "filters.location.allowed_countries") == ["US"]
    # quiet_hours is unset, so anything under it reads as None rather than raising
    assert value_at(cfg, "quiet_hours.start") is None


def test_value_at_formats_a_set_time_as_hh_mm_not_hh_mm_ss():
    """<input type="time"> both renders and submits "HH:MM" — str(time) would
    emit "HH:MM:SS", which never round-trips back equal to a submitted value
    (see save_section's changed-fields filter)."""
    from src.config import AppConfig
    from src.settings.fields import value_at
    cfg = AppConfig.model_validate({
        "quiet_hours": {"timezone": "America/Los_Angeles", "start": "22:00", "end": "07:00"},
    })
    assert value_at(cfg, "quiet_hours.start") == "22:00"
