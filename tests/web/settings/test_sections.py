"""The partition: every editable path belongs to exactly one place."""
from src.settings.fields import KIND_READ_ONLY, editable_fields, field_map
from src.web.settings.sections import (
    SECTIONS, advanced_group, advanced_groups, section_by_slug, section_fields,
)


def test_every_editable_path_is_owned_exactly_once():
    hand_built = [p for s in SECTIONS for p in s.paths]
    assert len(hand_built) == len(set(hand_built)), "a path is claimed twice"

    owned = set(hand_built)
    for group in advanced_groups():
        for spec in group.fields:
            assert spec.path not in owned, f"{spec.path} is in a section AND in Advanced"
            owned.add(spec.path)

    missing = {f.path for f in editable_fields()} - owned
    assert not missing, f"editable flags no page renders: {sorted(missing)}"


def test_sections_only_claim_paths_that_exist():
    known = set(field_map())
    for section in SECTIONS:
        unknown = set(section.paths) - known
        assert not unknown, f"{section.slug} claims missing paths: {sorted(unknown)}"


def test_advanced_renders_the_leftovers_of_a_partly_claimed_key():
    llm = section_by_slug("llm")
    assert "coach.provider" in llm.paths
    coach = advanced_group("coach")
    left = {f.path for f in coach.fields}
    assert "coach.provider" not in left
    assert "coach.max_jobs" in left


def test_a_fully_claimed_key_has_no_advanced_group():
    assert "filters" not in {g.key for g in advanced_groups()}


def test_structured_source_families_appear_read_only_in_advanced():
    sources = advanced_group("sources")
    workday = next(f for f in sources.fields if f.path == "sources.workday")
    assert workday.kind == KIND_READ_ONLY
    greenhouse = next(f for f in sources.fields if f.path == "sources.greenhouse")
    assert greenhouse.kind != KIND_READ_ONLY


def test_section_fields_are_returned_in_claim_order():
    filters = section_by_slug("filters")
    assert [f.path for f in section_fields(filters)] == list(filters.paths)


def test_nav_order_starts_at_overview_and_ends_at_advanced():
    assert SECTIONS[0].slug == "overview"
    assert SECTIONS[-1].slug == "advanced"


def test_secret_claims_are_real_secret_names():
    from src.config import Secrets
    for section in SECTIONS:
        unknown = set(section.secrets) - set(Secrets.model_fields)
        assert not unknown, f"{section.slug} claims unknown secrets: {sorted(unknown)}"
