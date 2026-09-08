import pytest

from src.geo import covers_any, geo_tags, resolve_geo_tags, validate_country_code


# ---- country name resolution ----
@pytest.mark.parametrize("loc,expected", [
    ("Remote, United States", {"country:us"}),
    ("Remote - US", {"country:us"}),
    ("U.S. only", {"country:us"}),
    ("Berlin, Germany", {"country:de"}),
    ("Onsite - London, UK", {"country:gb"}),
    ("Belfast, Northern Ireland", {"country:gb"}),   # must NOT also resolve ie
    ("Dublin, Ireland", {"country:ie"}),
    ("Amsterdam, Holland", {"country:nl"}),
    ("Sao Paulo, Brasil", {"country:br"}),
    ("Prague, Czech Republic", {"country:cz"}),
    ("Seoul, South Korea", {"country:kr"}),
    ("Dubai, UAE", {"country:ae"}),
    ("Toronto, Canada", {"country:ca"}),
])
def test_resolve_country_names(loc, expected):
    assert resolve_geo_tags(loc) == expected


# ---- ISO prefixes (Ashby/Workday style), uppercase-only ----
@pytest.mark.parametrize("loc,expected", [
    ("DE-Berlin", {"country:de"}),
    ("CH-Zurich-MSO", {"country:ch"}),
    ("PL-Warsaw", {"country:pl"}),
    ("US-WA-Bellevue", {"country:us"}),
    ("UK-London", {"country:gb"}),
    ("IN-Bangalore", {"country:in"}),
])
def test_resolve_iso_prefixes(loc, expected):
    assert resolve_geo_tags(loc) == expected


def test_iso_prefix_is_case_sensitive():
    # "In-Office" must not resolve to India; prefixes match uppercase only.
    assert resolve_geo_tags("In-Office - Dallas, TX") == set()


# ---- regions ----
@pytest.mark.parametrize("loc,expected", [
    ("Remote - Europe", {"region:europe"}),
    ("Remote (EU)", {"region:eu"}),
    ("Remote - EMEA", {"region:emea"}),
    ("Remote - APAC", {"region:apac"}),
    ("Remote, LATAM", {"region:latam"}),
    ("Remote, Latin America", {"region:latam"}),
    ("Remote, Worldwide", {"region:worldwide"}),
    ("Global - Remote", {"region:worldwide"}),
    ("Anywhere", {"region:worldwide"}),
    ("Remote - Middle East", {"region:middle_east"}),
])
def test_resolve_regions(loc, expected):
    assert resolve_geo_tags(loc) == expected


def test_resolve_multi_location_string():
    got = resolve_geo_tags("Germany, UK or US (Remote)")
    assert got == {"country:de", "country:gb", "country:us"}


def test_resolve_no_geo_signal():
    assert resolve_geo_tags("Dallas, TX (Hybrid)") == set()


# ---- covers_any ----
def test_covers_any_direct_country():
    assert covers_any({"country:us"}, {"us"})


def test_covers_any_disjoint_country():
    assert not covers_any({"country:de"}, {"us"})


def test_covers_any_region_contains_allowed():
    assert covers_any({"region:europe"}, {"de"})
    assert covers_any({"region:eu"}, {"de"})
    assert covers_any({"region:emea"}, {"de"})


def test_covers_any_region_excludes_allowed():
    assert not covers_any({"region:eu"}, {"us"})
    assert not covers_any({"region:apac"}, {"de"})


def test_covers_any_worldwide_covers_everyone():
    assert covers_any({"region:worldwide"}, {"us"})
    assert covers_any({"region:worldwide"}, {"de"})


def test_covers_any_empty_geo_is_false():
    assert not covers_any(set(), {"us"})


def test_covers_any_explicit_country_suppresses_worldwide():
    # "Remote - Anywhere in the U.S." → {country:us, region:worldwide}: the
    # explicit country wins over the generic Anywhere/Worldwide token.
    assert not covers_any({"country:us", "region:worldwide"}, {"de"})
    assert covers_any({"country:us", "region:worldwide"}, {"us"})


def test_covers_any_worldwide_alone_still_covers_all():
    assert covers_any({"region:worldwide"}, {"de"})
    assert covers_any({"region:worldwide"}, {"us"})


def test_covers_any_unknown_region_grants_nothing():
    assert not covers_any({"region:atlantis"}, {"us"})


def test_geo_tags_extracts_only_geo():
    tags = {"remote", "dallas", "country:us", "region:eu", "onsite_only"}
    assert geo_tags(tags) == {"country:us", "region:eu"}


# ---- validate_country_code ----
def test_validate_country_code_canonicalizes_case():
    assert validate_country_code("us") == "US"
    assert validate_country_code("De") == "DE"


def test_validate_country_code_uk_alias():
    assert validate_country_code("UK") == "GB"
    assert validate_country_code("uk") == "GB"


def test_validate_country_code_unknown_raises():
    with pytest.raises(ValueError, match="XX"):
        validate_country_code("XX")
