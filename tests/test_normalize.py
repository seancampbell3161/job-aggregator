from datetime import datetime, timezone

import pytest

from src.models import RawPosting
from src.normalize import (
    normalize,
    normalize_employment_type,
    workplace_type_from_tags,
)


def _raw(**kw):
    base = dict(
        source="greenhouse:stripe",
        external_id="1",
        title="Software Engineer",
        description="",
        apply_url="https://x",
        location=None,
        remote=None,
        department=None,
        posted_at=datetime(2026, 4, 30, tzinfo=timezone.utc),
    )
    base.update(kw)
    return RawPosting(**base)


STACK_KEYWORDS = ["c#", ".net", "react", "angular", "go", "golang", "python", "next.js"]


@pytest.mark.parametrize(
    "raw,expected",
    [
        # Hiring.cafe / Lever real values
        ("Full Time", "full_time"),
        ("Full-time", "full_time"),
        ("Contract", "contract"),
        ("Part Time", "part_time"),
        ("Part-time", "part_time"),
        ("Internship", "internship"),
        ("Temporary", "temporary"),
        ("Permanent", "full_time"),
        # contract-to-hire is accepted by the profile → its own bucket
        ("Contract-to-hire", "contract_to_hire"),
        ("Contract to Hire", "contract_to_hire"),
        ("Temp-to-perm", "contract_to_hire"),
        # agency/consulting/1099 collapse to contract (a profile hard-no)
        ("1099", "contract"),
        ("Freelance", "contract"),
        ("Consulting", "contract"),
        # unknown / empty → None (filter fails open)
        (None, None),
        ("", None),
        ("   ", None),
        ("Something Weird", None),
    ],
)
def test_normalize_employment_type(raw, expected):
    assert normalize_employment_type(raw) == expected


def test_normalize_maps_raw_commitment_onto_posting():
    p = normalize(_raw(employment_type="Contract"), stack_keywords=STACK_KEYWORDS)
    assert p.employment_type == "contract"


def test_normalize_employment_type_defaults_none_when_absent():
    p = normalize(_raw(), stack_keywords=STACK_KEYWORDS)
    assert p.employment_type is None


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Senior Backend Engineer", "senior"),
        ("Staff Software Engineer", "staff"),
        ("Junior Engineer", "junior"),
        ("Engineering Manager", "manager"),
        ("Software Engineer", None),
        ("VP of Engineering", "manager"),
        ("Principal Architect", "staff"),
        ("Staff Engineer", "staff"),
        # "Member of Technical Staff" is a base IC title, not staff-level — the
        # "staff" seniority bucket must not fire on it (2026-07-02 carve-out).
        ("Member of Technical Staff", None),
        ("Senior Member of Technical Staff", "senior"),
    ],
)
def test_normalize_seniority(title, expected):
    out = normalize(_raw(title=title), stack_keywords=STACK_KEYWORDS)
    assert out.seniority == expected


@pytest.mark.parametrize(
    "loc,remote,expected_tags",
    [
        ("Remote, United States", None, {"remote"}),
        ("Hybrid - San Francisco, CA", None, {"hybrid"}),
        (None, None, {"unknown_location"}),
        ("San Francisco, CA", False, {"onsite_only"}),
        (None, True, {"remote"}),
    ],
)
def test_normalize_location_tags(loc, remote, expected_tags):
    out = normalize(_raw(location=loc, remote=remote), stack_keywords=STACK_KEYWORDS, allowed_cities=[])
    assert expected_tags.issubset(out.location_tags)


@pytest.mark.parametrize("loc,expected_tags", [
    ("Remote, United States", {"remote", "country:us"}),
    ("Remote - US", {"remote", "country:us"}),
    ("Remote, Worldwide", {"remote", "region:worldwide"}),
    ("Remote, Europe", {"remote", "region:europe"}),
    ("Remote, UK only", {"remote", "country:gb"}),
    ("Hybrid - Seattle, WA", {"hybrid", "seattle"}),
    ("Dallas, TX", {"dallas"}),
    ("Denver, CO (Hybrid)", {"hybrid", "denver"}),
    ("New York, NY", {"onsite_only"}),
    ("San Francisco, CA", {"onsite_only"}),
    ("Onsite - London, UK", {"onsite_only", "country:gb"}),
    # Ashby/Workday-style ISO-region prefixes
    ("CH-Zurich-MSO", {"onsite_only", "country:ch"}),
    ("PL-Warsaw", {"onsite_only", "country:pl"}),
    ("DE-Berlin", {"onsite_only", "country:de"}),
    ("FR-Paris", {"onsite_only", "country:fr"}),
    ("IN-Bangalore", {"onsite_only", "country:in"}),
    ("US-WA-Bellevue", {"onsite_only", "country:us"}),
    ("US-CA-San Francisco", {"onsite_only", "country:us"}),
])
def test_normalize_geo_tags(loc, expected_tags):
    out = normalize(_raw(location=loc), stack_keywords=STACK_KEYWORDS, allowed_cities=["dallas", "seattle", "denver"])
    assert expected_tags.issubset(out.location_tags)


# Specific country names that previously slipped through as "unknown" — these
# came from real Twilio postings the user saw alerted incorrectly.
@pytest.mark.parametrize("loc,country_tag", [
    ("Remote - Spain", "country:es"),
    ("Remote - Ireland", "country:ie"),
    ("Remote - Estonia", "country:ee"),
    ("Remote - Germany", "country:de"),
    ("Remote - France", "country:fr"),
    ("Remote - Netherlands", "country:nl"),
    ("Remote - Poland", "country:pl"),
    ("Remote - Portugal", "country:pt"),
    ("Remote - Italy", "country:it"),
    ("Remote - Sweden", "country:se"),
    ("Remote - Brazil", "country:br"),
    ("Remote - Argentina", "country:ar"),
    ("Remote - Japan", "country:jp"),
    ("Remote - Israel", "country:il"),
    ("Remote - South Africa", "country:za"),
    ("Remote - Vietnam", "country:vn"),
    ("Remote - Philippines", "country:ph"),
])
def test_normalize_tags_explicit_foreign_countries(loc, country_tag):
    out = normalize(_raw(location=loc), stack_keywords=STACK_KEYWORDS, allowed_cities=["dallas", "seattle", "denver"])
    assert country_tag in out.location_tags, f"Expected {country_tag} for {loc!r}, got {set(out.location_tags)}"
    assert "remote" in out.location_tags
    assert "country:us" not in out.location_tags


@pytest.mark.parametrize("loc", [
    "CH-Zurich-MSO",
    "PL-Warsaw",
    "Paris",
    "San Francisco",
    "San Francisco, CA",
])
def test_normalize_ashby_isremote_none_is_onsite_not_unknown(loc):
    """Ashby returns isRemote=None when no remote flag is set on the listing.
    Combined with a location string, that posting must be tagged onsite_only —
    not unknown_location — so allow_unknown=true doesn't leak it through."""
    out = normalize(
        _raw(location=loc, remote=None),
        stack_keywords=STACK_KEYWORDS,
        allowed_cities=["dallas", "seattle", "denver"],
    )
    assert "unknown_location" not in out.location_tags
    assert "onsite_only" in out.location_tags


def test_normalize_stack_extraction_word_boundary_for_go():
    out = normalize(
        _raw(title="Backend Engineer", description="We use Golang and Python."),
        stack_keywords=STACK_KEYWORDS,
    )
    assert "go" in out.stack
    assert "python" in out.stack


def test_normalize_stack_does_not_match_go_inside_other_word():
    out = normalize(
        _raw(title="Engineer", description="We build cargo systems for ago lookalike."),
        stack_keywords=STACK_KEYWORDS,
    )
    assert "go" not in out.stack


def test_normalize_stack_dotnet_and_csharp():
    out = normalize(
        _raw(title="Engineer", description="C# .NET shop, no Java"),
        stack_keywords=STACK_KEYWORDS,
    )
    assert "c#" in out.stack
    assert ".net" in out.stack


def test_normalize_company_from_source():
    out = normalize(_raw(source="greenhouse:stripe"), stack_keywords=STACK_KEYWORDS)
    assert out.company == "Stripe"


def test_normalize_company_from_hn_title():
    out = normalize(
        _raw(source="hn:who_is_hiring", title="Acme Co | Backend Engineer"),
        stack_keywords=STACK_KEYWORDS,
    )
    assert out.company == "Acme Co"


def test_normalize_company_prefers_raw_company_field():
    """ORC tenants set raw.company so postings surface with a real display
    name instead of the opaque tenant slug derived from the source string."""
    out = normalize(
        _raw(source="oraclecloud:egug:CX_1", company="American Express"),
        stack_keywords=STACK_KEYWORDS,
    )
    assert out.company == "American Express"


def test_normalize_company_falls_back_to_source_when_raw_company_unset():
    """Tenant-shaped source: the name is the tenant, not the career-site id."""
    out = normalize(
        _raw(source="oraclecloud:egug:CX_1"),
        stack_keywords=STACK_KEYWORDS,
    )
    assert out.company == "Egug"


def test_normalize_company_from_workday_tenant_drops_site_segment():
    out = normalize(_raw(source="workday:paypal:jobs"), stack_keywords=STACK_KEYWORDS)
    assert out.company == "Paypal"


def test_normalize_company_from_nested_ats_source_is_the_employer():
    """hiringcafe wraps another ATS: "{connector}:{ats_family}:{slug}". The
    employer is the trailing slug — reading the middle segment would file every
    Ashby-hosted startup under a single "Ashby" pseudo-company."""
    out = normalize(_raw(source="hiringcafe:ashby:mercor"), stack_keywords=STACK_KEYWORDS)
    assert out.company == "Mercor"

    underscored = normalize(
        _raw(source="hiringcafe:adhoc:yum_brands"), stack_keywords=STACK_KEYWORDS
    )
    assert underscored.company == "Yum Brands"


def test_normalize_company_nested_ats_distinguishes_sibling_slugs():
    """Two employers on the same wrapped ATS must not collapse together."""
    a = normalize(_raw(source="hiringcafe:ashby:mercor"), stack_keywords=STACK_KEYWORDS)
    b = normalize(_raw(source="hiringcafe:ashby:browserbase"), stack_keywords=STACK_KEYWORDS)
    assert a.company != b.company


def test_normalize_job_id_format():
    out = normalize(
        _raw(source="greenhouse:stripe", external_id="42"),
        stack_keywords=STACK_KEYWORDS,
    )
    assert out.job_id == "greenhouse:stripe:42"


def test_normalize_rippling_location_labels():
    cities = ["dallas", "seattle", "denver", "san francisco", "portland"]

    remote_us = normalize(_raw(source="rippling:acme", location="Remote (United States)"),
                          stack_keywords=STACK_KEYWORDS, allowed_cities=cities)
    assert "remote" in remote_us.location_tags and "country:us" in remote_us.location_tags

    sf = normalize(_raw(source="rippling:acme", location="San Francisco, CA"),
                   stack_keywords=STACK_KEYWORDS, allowed_cities=cities)
    assert "san francisco" in sf.location_tags

    blr = normalize(_raw(source="rippling:acme", location="Bangalore, India"),
                    stack_keywords=STACK_KEYWORDS, allowed_cities=cities)
    assert "country:in" in blr.location_tags

    hyb = normalize(_raw(source="rippling:acme", location="Hybrid (San Francisco, California, US)"),
                    stack_keywords=STACK_KEYWORDS, allowed_cities=cities)
    assert "hybrid" in hyb.location_tags and "san francisco" in hyb.location_tags

    # company derived from the slug
    assert remote_us.company == "Acme"


@pytest.mark.parametrize("tags,expected", [
    (frozenset({"remote"}), "remote"),
    (frozenset({"hybrid"}), "hybrid"),
    (frozenset({"onsite_only"}), "onsite"),
    (frozenset({"unknown_location"}), "unknown"),
    (frozenset({"remote", "hybrid"}), "hybrid"),                  # hybrid wins over remote
    (frozenset({"remote", "seattle", "country:us"}), "remote"),  # ignores geo/city tags
    (frozenset({"seattle", "country:us"}), None),                # geo only → no workplace signal
    (frozenset(), None),
])
def test_workplace_type_from_tags(tags, expected):
    assert workplace_type_from_tags(tags) == expected


def test_null_title_does_not_crash():
    """Oracle Cloud sends an explicit JSON null for Title on some requisitions.
    RawPosting is an unvalidated dataclass, so None reaches normalize() despite
    the `str` annotation — it used to raise out of the ats cycle every 10 min."""
    n = normalize(_raw(title=None), stack_keywords=STACK_KEYWORDS, allowed_cities=[])
    assert n.title == ""
    assert n.seniority is None
    assert n.job_id == "greenhouse:stripe:1"
    # company still derives from the slug rather than blowing up on the title
    assert n.company == "Stripe"


def test_null_title_hn_company_derivation():
    """_company() splits the title for hn: sources — the None path must not crash."""
    n = normalize(_raw(source="hn:whoishiring", title=None),
                  stack_keywords=STACK_KEYWORDS, allowed_cities=[])
    assert n.company == "unknown"
