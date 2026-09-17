"""Form body -> dotted-path patch. The two behaviours worth guarding: an
unchecked box submits nothing, and setting a default removes the key."""
import pytest

from src.settings.errors import SettingsInvalid
from src.settings.fields import (
    KIND_BOOL, KIND_CHIPS, KIND_CHOICE, KIND_INT, KIND_MULTI_CHOICE,
    KIND_READ_ONLY, KIND_TEXT, FieldSpec,
)
from src.web.settings.forms import GROUP_TOGGLE_SUFFIX, apply_patch, decode, errors_by_path


def f(path, kind, **kw):
    return FieldSpec(path=path, kind=kind, **kw)


def test_unchecked_box_decodes_to_false_not_missing():
    fields = [f("discovery.enabled", KIND_BOOL, default=True)]
    assert decode(fields, {}) == {"discovery.enabled": False}
    assert decode(fields, {"discovery.enabled": ["on"]}) == {"discovery.enabled": True}


def test_int_and_optional_int():
    fields = [
        f("a.n", KIND_INT, default=1),
        f("a.opt", KIND_INT, default=None, optional=True),
    ]
    out = decode(fields, {"a.n": ["7"], "a.opt": [""]})
    assert out == {"a.n": 7, "a.opt": None}


def test_unparseable_int_is_reported_as_a_field_error():
    with pytest.raises(SettingsInvalid) as exc:
        decode([f("a.n", KIND_INT, default=1)], {"a.n": ["twelve"]})
    assert exc.value.errors == [{"loc": "a.n", "msg": "must be a whole number"}]


def test_text_and_optional_text():
    fields = [
        f("a.s", KIND_TEXT, default=""),
        f("a.o", KIND_TEXT, default=None, optional=True),
    ]
    assert decode(fields, {"a.s": [""], "a.o": [""]}) == {"a.s": "", "a.o": None}


def test_chips_drop_blanks_and_strip():
    out = decode([f("s.greenhouse", KIND_CHIPS, default=[])],
                 {"s.greenhouse": [" stripe ", "", "figma"]})
    assert out == {"s.greenhouse": ["stripe", "figma"]}


def test_multi_choice_collects_checked_values():
    spec = f("f.seniority_allow", KIND_MULTI_CHOICE, default=["mid"],
             choices=("junior", "mid", "senior"))
    assert decode([spec], {"f.seniority_allow": ["mid", "senior"]}) == {
        "f.seniority_allow": ["mid", "senior"]}
    assert decode([spec], {}) == {"f.seniority_allow": []}


def test_choice_passes_through():
    spec = f("r.provider", KIND_CHOICE, default="anthropic", choices=("anthropic", "gemini"))
    assert decode([spec], {"r.provider": ["gemini"]}) == {"r.provider": "gemini"}


def test_read_only_and_non_editable_fields_are_skipped():
    fields = [
        f("s.workday", KIND_READ_ONLY, default=[]),
        f("secrets.x", KIND_TEXT, default="", editable=False),
        f("a.s", KIND_TEXT, default=""),
    ]
    assert decode(fields, {"a.s": ["v"]}) == {"a.s": "v"}


def test_disabled_optional_group_patches_the_group_away():
    fields = [f("quiet_hours.start", KIND_TEXT, default="")]
    out = decode(fields, {}, optional_groups={"quiet_hours"})
    assert out == {"quiet_hours": None}


def test_enabled_optional_group_decodes_its_members():
    fields = [f("quiet_hours.start", KIND_TEXT, default="")]
    out = decode(fields, {f"quiet_hours{GROUP_TOGGLE_SUFFIX}": ["on"],
                          "quiet_hours.start": ["22:00"]},
                 optional_groups={"quiet_hours"})
    assert out == {"quiet_hours.start": "22:00"}


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


def test_errors_split_into_field_and_form_level():
    exc = SettingsInvalid([
        {"loc": "filters.comp_floor_usd", "msg": "must be >= 0"},
        {"loc": "", "msg": "document is not a mapping"},
        {"loc": "schedules.digest_cron", "msg": "bad crontab"},
    ])
    by_path, form_level = errors_by_path(exc, known={"filters.comp_floor_usd"})
    assert by_path == {"filters.comp_floor_usd": "must be >= 0"}
    assert form_level == ["document is not a mapping", "schedules.digest_cron: bad crontab"]
