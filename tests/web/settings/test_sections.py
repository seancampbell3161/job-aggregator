"""The partition: every editable path belongs to exactly one place."""
from src.settings.fields import KIND_ROWS, editable_fields, field_map
from src.web.settings.sections import (
    SECTIONS, UNCLAIMED_SECRETS, advanced_group, advanced_groups, section_by_slug,
    section_fields,
)


def test_no_path_is_claimed_by_two_sections():
    hand_built = [p for s in SECTIONS for p in s.paths]
    assert len(hand_built) == len(set(hand_built))


def test_advanced_groups_are_exactly_the_keys_with_leftovers():
    """Coverage itself is guaranteed by construction — _groups() builds Advanced
    as the complement of CLAIMED_PATHS, so a coverage assertion cannot fail.
    This pins the split instead: it breaks the moment a section's claims change
    which keys still have leftovers."""
    assert [g.key for g in advanced_groups()] == [
        "sources", "discovery", "gap_analysis", "tailoring", "board",
        "audit", "coach", "ops_notify", "http", "gmail",
    ]


def test_advanced_never_renders_a_claimed_path():
    claimed = {p for s in SECTIONS for p in s.paths}
    for group in advanced_groups():
        overlap = {f.path for f in group.fields} & claimed
        assert not overlap, f"{group.key} re-renders claimed paths: {sorted(overlap)}"


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


def test_structured_source_families_appear_as_rows_in_advanced():
    sources = advanced_group("sources")
    workday = next(f for f in sources.fields if f.path == "sources.workday")
    assert workday.kind == KIND_ROWS
    greenhouse = next(f for f in sources.fields if f.path == "sources.greenhouse")
    assert greenhouse.kind != KIND_ROWS


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


def test_every_secret_is_claimed_or_explicitly_exempt():
    from src.config import Secrets
    claimed = {name for s in SECTIONS for name in s.secrets}
    assert claimed | UNCLAIMED_SECRETS == set(Secrets.model_fields)
    assert not (claimed & UNCLAIMED_SECRETS)
