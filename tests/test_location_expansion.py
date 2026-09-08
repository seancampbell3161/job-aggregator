"""End-to-end guards for the allowed_cities expansion (any workplace type in
acceptable cities), using a representative city list inlined below. The real
config.yaml is personal and has been untracked since the 2026-07-14
untracked-personal-config change, so it is no longer read here — the
typo'd-city guard now lives with the config owner, not CI. ALLOWED_CITIES is
an inline snapshot of the allowed_cities list that was live on 2026-07-02
(git show 60d7c80:config.yaml)."""
from datetime import datetime, timezone

import pytest

from src.config import FiltersConfig, LocationFilterConfig
from src.filters import Verdict, filter_location
from src.models import RawPosting
from src.normalize import normalize

STACK_KEYWORDS = ["c#", ".net", "react", "angular", "go", "golang", "python", "next.js"]

ALLOWED_CITIES = [
    "dallas",
    "austin",
    "seattle",
    "bellevue",
    "redmond",
    "denver",
    "san francisco",
    "san jose",
    "palo alto",
    "mountain view",
    "sunnyvale",
    "portland",
    "charlotte",
    "new york",
    "nyc",
]


def _config_cities() -> list[str]:
    return ALLOWED_CITIES


def _raw(location: str) -> RawPosting:
    return RawPosting(
        source="greenhouse:acme",
        external_id="1",
        title="Senior Software Engineer",
        description="",
        apply_url="https://x",
        location=location,
        remote=None,
        posted_at=datetime(2026, 4, 30, tzinfo=timezone.utc),
    )


def _filters_from_config() -> FiltersConfig:
    return FiltersConfig(
        titles=["software engineer"],
        seniority_allow=["mid", "senior"],
        location=LocationFilterConfig(
            remote_must_be_us=True,
            allowed_cities=_config_cities(),
            allow_unknown=True,
        ),
        comp_floor_usd=130_000,
        stack_any_of=["python"],
    )


@pytest.mark.parametrize("loc", [
    "Austin, TX (Onsite)",
    "Charlotte, NC (Hybrid)",
    "New York, NY (Onsite)",
    "NYC",
    "Bellevue, WA",
    "Redmond, WA",
    "San Jose, CA (Onsite)",
    "Palo Alto, CA (Hybrid)",
    "Mountain View, CA",
    "Sunnyvale, CA",
])
def test_onsite_hybrid_in_new_cities_match(loc):
    out = normalize(_raw(loc), stack_keywords=STACK_KEYWORDS, allowed_cities=_config_cities())
    assert filter_location(out, _filters_from_config()) is Verdict.MATCH


def test_onsite_in_unlisted_us_city_rejected():
    # Atlanta is US but not an allowed city — the city gate must still reject it.
    out = normalize(_raw("Atlanta, GA (Onsite)"), stack_keywords=STACK_KEYWORDS, allowed_cities=_config_cities())
    assert filter_location(out, _filters_from_config()) is Verdict.REJECT


def test_charlotte_does_not_match_charlottesville():
    out = normalize(_raw("Charlottesville, VA (Onsite)"), stack_keywords=STACK_KEYWORDS, allowed_cities=_config_cities())
    assert "charlotte" not in out.location_tags
    assert filter_location(out, _filters_from_config()) is Verdict.REJECT


def test_new_york_does_not_match_newark():
    out = normalize(_raw("Newark, NJ (Onsite)"), stack_keywords=STACK_KEYWORDS, allowed_cities=_config_cities())
    assert "new york" not in out.location_tags
    assert filter_location(out, _filters_from_config()) is Verdict.REJECT


@pytest.mark.parametrize("loc,foreign_tag", [
    ("San Jose, Costa Rica", "country:cr"),
    ("San Jose, Costa Rica (Hybrid)", "country:cr"),
    ("Mountain View, Canada", "country:ca"),
    ("Portland, Australia", "country:au"),
])
def test_us_city_name_in_foreign_country_rejected(loc, foreign_tag):
    # A US city name colliding with a foreign locale must NOT match on the city
    # tag — the resolved foreign country disqualifies it.
    out = normalize(_raw(loc), stack_keywords=STACK_KEYWORDS, allowed_cities=_config_cities())
    assert foreign_tag in out.location_tags, f"expected {foreign_tag} for {loc!r}, got {set(out.location_tags)}"
    assert filter_location(out, _filters_from_config()) is Verdict.REJECT


def test_remote_with_allowed_city_and_foreign_country_rejected():
    # Deviation #3 (see spec Parity deviations): the old city→us inference
    # made this MATCH; the coverage rule sees country:gb and rejects.
    out = normalize(_raw("Remote - Dallas, TX; London, UK"), stack_keywords=STACK_KEYWORDS, allowed_cities=_config_cities())
    assert filter_location(out, _filters_from_config()) is Verdict.REJECT


def test_remote_foreign_city_collision_rejected():
    # The false positive the same change fixed: San Jose, Costa Rica used to
    # MATCH via the city-implied us tag.
    out = normalize(_raw("San Jose, Costa Rica (Remote)"), stack_keywords=STACK_KEYWORDS, allowed_cities=_config_cities())
    assert filter_location(out, _filters_from_config()) is Verdict.REJECT
