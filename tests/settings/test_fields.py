"""The AppConfig walker that both the settings UI and the docs guard read."""
from src.settings.fields import (
    KIND_BOOL, KIND_CHIPS, KIND_CHOICE, KIND_INT, KIND_MULTI_CHOICE,
    KIND_READ_ONLY, KIND_TEXT, KIND_TIME,
    all_paths, editable_fields, field_map, optional_groups,
)


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


def test_list_of_model_is_read_only():
    assert field_map()["sources.workday"].kind == KIND_READ_ONLY


def test_mixed_list_is_read_only():
    assert field_map()["sources.hiringcafe.extra_queries"].kind == KIND_READ_ONLY


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
