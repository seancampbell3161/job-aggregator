"""Six questions — only the things a résumé cannot tell you. Each one binds to
a real config path so the draft is grounded rather than invented."""
import pytest

from src.config import AppConfig
from src.resume_intake.interview import (
    INTERVIEW_FIELDS, answers_to_patch, decode_answers,
)

ANSWERS = {
    "target_titles": ["platform engineer", "staff engineer"],
    "seniority": ["senior", "staff"],
    "ic_or_management": ["ic"],
    "countries": ["US"],
    "cities": ["Seattle"],
    "remote_policy": ["allowed_countries"],
    "employment_types": ["full_time"],
    "comp_floor": ["185000"],
}


def test_every_field_with_a_path_targets_a_real_config_path():
    """A typo here would silently drop the answer on the floor."""
    from src.settings.fields import field_map
    known = set(field_map())
    for f in INTERVIEW_FIELDS:
        if f.path is not None:
            assert f.path in known, f.path


def test_the_patch_only_touches_filter_paths():
    patch = answers_to_patch(decode_answers(ANSWERS))
    assert all(p.startswith("filters.") for p in patch)


def test_titles_seniority_and_comp():
    patch = answers_to_patch(decode_answers(ANSWERS))
    assert patch["filters.titles"] == ["platform engineer", "staff engineer"]
    assert patch["filters.seniority_allow"] == ["senior", "staff"]
    assert patch["filters.comp_floor_usd"] == 185000


def test_location_answers_map_onto_the_location_section():
    patch = answers_to_patch(decode_answers(ANSWERS))
    assert patch["filters.location.allowed_countries"] == ["US"]
    assert patch["filters.location.allowed_cities"] == ["Seattle"]
    assert patch["filters.location.remote_policy"] == "allowed_countries"


def test_employment_types_are_INVERTED_into_blocked_types():
    """The form asks what you WANT; the config stores what is BLOCKED."""
    patch = answers_to_patch(decode_answers(ANSWERS))
    blocked = patch["filters.blocked_employment_types"]
    assert "full_time" not in blocked
    assert set(blocked) == {"part_time", "contract", "contract_to_hire", "temporary", "internship"}


def test_wanting_everything_blocks_nothing():
    answers = decode_answers({**ANSWERS, "employment_types": [
        "full_time", "part_time", "contract", "contract_to_hire", "temporary", "internship",
    ]})
    assert answers_to_patch(answers)["filters.blocked_employment_types"] == []


def test_ic_or_management_writes_no_path():
    """There is no seniority/IC filter — it only shapes the profile prose."""
    assert all(f.path is None for f in INTERVIEW_FIELDS if f.name == "ic_or_management")
    assert "ic_or_management" not in answers_to_patch(decode_answers(ANSWERS))


def test_blank_answers_produce_no_patch_entries():
    patch = answers_to_patch(decode_answers({}))
    assert patch == {}


def test_blank_comp_floor_is_omitted_not_zero():
    answers = decode_answers({**ANSWERS, "comp_floor": [""]})
    assert "filters.comp_floor_usd" not in answers_to_patch(answers)


def test_non_numeric_comp_floor_is_omitted():
    answers = decode_answers({**ANSWERS, "comp_floor": ["a lot"]})
    assert "filters.comp_floor_usd" not in answers_to_patch(answers)


def test_the_patch_validates_against_the_real_model():
    """The strongest guarantee: whatever the interview produces must be a
    settings document the app will accept."""
    from src.settings.service import canonical_doc
    from src.settings.patch import apply_patch
    doc = canonical_doc(AppConfig())
    apply_patch(doc, answers_to_patch(decode_answers(ANSWERS)))
    cfg = AppConfig.model_validate(doc)
    assert cfg.filters.titles == ["platform engineer", "staff engineer"]
    assert cfg.filters.comp_floor_usd == 185000
