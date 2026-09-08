from datetime import datetime, timedelta, timezone

import pytest
from freezegun import freeze_time

from src.config import FiltersConfig, LocationFilterConfig
from src.filters import (
    Verdict, evaluate, filter_age, filter_role, filter_seniority,
    filter_location, filter_stack, filter_comp, filter_employment_type,
    filter_company,
)
from src.models import NormalizedPosting


def _filters(**override) -> FiltersConfig:
    base = dict(
        titles=["software engineer", "backend engineer", "fullstack engineer", "full stack engineer", "full-stack engineer"],
        seniority_allow=["mid", "senior"],
        location=LocationFilterConfig(allowed_countries=["US"], allowed_cities=["dallas", "seattle", "denver"], allow_unknown=True),
        comp_floor_usd=120_000,
        stack_any_of=["c#", ".net", "react", "angular", "go", "golang", "python", "next.js"],
    )
    base.update(override)
    return FiltersConfig(**base)


def _post(**kw) -> NormalizedPosting:
    base = dict(
        job_id="greenhouse:stripe:1",
        title="Senior Backend Engineer",
        company="Stripe",
        location_text="Remote",
        location_tags=frozenset({"remote"}),
        seniority="senior",
        stack=frozenset({"python", "go"}),
        comp_min=180_000,
        comp_max=240_000,
        apply_url="https://x",
        description="",
        posted_at=datetime(2026, 4, 30, tzinfo=timezone.utc),
        source="greenhouse:stripe",
    )
    base.update(kw)
    return NormalizedPosting(**base)


# ---- role ----
def test_role_match_backend():
    assert filter_role(_post(title="Senior Backend Engineer"), _filters()) is Verdict.MATCH


def test_role_match_fullstack_variants():
    f = _filters()
    for t in ["Fullstack Engineer", "Full Stack Engineer", "Full-stack Software Engineer"]:
        assert filter_role(_post(title=t), f) is Verdict.MATCH


def test_role_reject_unrelated():
    assert filter_role(_post(title="Office Manager"), _filters()) is Verdict.REJECT


# ---- company ----
def test_company_gate_disabled_by_default():
    assert filter_company(_post(company="Microsoft"), _filters()) is Verdict.MATCH


def test_company_reject_exact():
    f = _filters(blocked_companies=["microsoft"])
    assert filter_company(_post(company="Microsoft"), f) is Verdict.REJECT


def test_company_reject_is_case_and_punctuation_insensitive():
    f = _filters(blocked_companies=["Microsoft"])
    for name in ("microsoft", "MICROSOFT", "Microsoft, Inc.", "Microsoft Corporation"):
        assert filter_company(_post(company=name), f) is Verdict.REJECT


def test_company_reject_slug_derived_names():
    # Connectors with no company field fall back to the board slug
    # (normalize._company): "workday:paypal:jobs" -> "Paypal:Jobs".
    f = _filters(blocked_companies=["microsoft", "paypal", "amazon", "google", "apple"])
    for name in ("Eightfold:Microsoft", "Paypal:Jobs", "Adhoc:Amazon", "Google:Google", "Adhoc:Apple"):
        assert filter_company(_post(company=name), f) is Verdict.REJECT


def test_company_reject_matches_subsidiary_prefix():
    f = _filters(blocked_companies=["amazon"])
    for name in ("Amazon Neptune", "Amazon DynamoDB", "Amazon Advertising"):
        assert filter_company(_post(company=name), f) is Verdict.REJECT


def test_company_does_not_substring_match_a_longer_word():
    # The whole-word rule is the point: "apple" must not swallow "Applebee's",
    # nor "uber" swallow "Uberflip".
    f = _filters(blocked_companies=["apple", "uber"])
    for name in ("Applebee's", "Applied Intuition", "Uberflip", "Uberall"):
        assert filter_company(_post(company=name), f) is Verdict.MATCH


def test_company_multi_word_entry_matches_as_phrase():
    f = _filters(blocked_companies=["career launch"])
    assert filter_company(_post(company="Career Launch"), f) is Verdict.REJECT
    assert filter_company(_post(company="Career Launch LLC"), f) is Verdict.REJECT
    # Same words, not consecutive -> no match.
    assert filter_company(_post(company="Career Services Launch"), f) is Verdict.MATCH


def test_company_unmatched_passes():
    f = _filters(blocked_companies=["microsoft"])
    assert filter_company(_post(company="Stripe"), f) is Verdict.MATCH


def test_company_missing_name_fails_open():
    f = _filters(blocked_companies=["microsoft"])
    assert filter_company(_post(company=""), f) is Verdict.MATCH


def test_company_blank_denylist_entry_does_not_block_everything():
    f = _filters(blocked_companies=["", "   "])
    assert filter_company(_post(company="Stripe"), f) is Verdict.MATCH


def test_evaluate_rejects_blocked_company_and_attributes_the_gate():
    f = _filters(blocked_companies=["microsoft"])
    d = evaluate(_post(company="Microsoft Corporation"), f)
    assert d.allow is False
    assert d.rejected_by == "company"


def test_evaluate_company_gate_precedes_role():
    # A blocked company with an off-list title is still credited to `company`,
    # since the gate runs ahead of the role filter.
    f = _filters(blocked_companies=["microsoft"])
    d = evaluate(_post(company="Microsoft", title="Product Manager"), f)
    assert d.rejected_by == "company"


# ---- employment_type ----
def test_employment_type_reject_contract():
    assert filter_employment_type(_post(employment_type="contract"), _filters()) is Verdict.REJECT


def test_employment_type_reject_temporary_and_intern_and_parttime():
    f = _filters()
    for et in ("temporary", "part_time", "internship"):
        assert filter_employment_type(_post(employment_type=et), f) is Verdict.REJECT


def test_employment_type_allow_full_time():
    assert filter_employment_type(_post(employment_type="full_time"), _filters()) is Verdict.MATCH


def test_employment_type_allow_contract_to_hire():
    # Profile accepts contract-to-hire; it is not in the default block list.
    assert filter_employment_type(_post(employment_type="contract_to_hire"), _filters()) is Verdict.MATCH


def test_employment_type_unknown_fails_open():
    assert filter_employment_type(_post(employment_type=None), _filters()) is Verdict.UNKNOWN


def test_employment_type_respects_config_override():
    # An empty block list disables the gate — even a known contract passes.
    f = _filters(blocked_employment_types=[])
    assert filter_employment_type(_post(employment_type="contract"), f) is Verdict.MATCH


def test_evaluate_rejects_contract_labeled_employment_type():
    # A posting that would otherwise pass every gate is dropped, and the audit
    # trail attributes it to the employment_type filter.
    d = evaluate(_post(employment_type="contract"), _filters())
    assert d.allow is False
    assert d.rejected_by == "employment_type"


# ---- seniority ----
def test_seniority_match_senior():
    assert filter_seniority(_post(seniority="senior"), _filters()) is Verdict.MATCH


def test_seniority_reject_junior():
    assert filter_seniority(_post(seniority="junior"), _filters()) is Verdict.REJECT


def test_seniority_reject_staff():
    assert filter_seniority(_post(seniority="staff"), _filters()) is Verdict.REJECT


def test_seniority_reject_manager():
    assert filter_seniority(_post(seniority="manager"), _filters()) is Verdict.REJECT


def test_seniority_none_treated_as_mid_match():
    assert filter_seniority(_post(seniority=None), _filters()) is Verdict.MATCH


# ---- location ----
def test_location_match_remote_us():
    assert filter_location(_post(location_tags=frozenset({"remote", "country:us"})), _filters()) is Verdict.MATCH


def test_location_reject_remote_foreign_country():
    # Remote, Germany-only, with allowed_countries=[US] → REJECT
    assert filter_location(_post(location_tags=frozenset({"remote", "country:de"})), _filters()) is Verdict.REJECT


def test_location_reject_remote_foreign_region():
    # Remote — EU with allowed_countries=[US] → REJECT (region doesn't cover US)
    assert filter_location(_post(location_tags=frozenset({"remote", "region:eu"})), _filters()) is Verdict.REJECT


def test_location_match_remote_worldwide():
    assert filter_location(_post(location_tags=frozenset({"remote", "region:worldwide"})), _filters()) is Verdict.MATCH


def test_location_match_remote_region_containment():
    # Remote — Europe with allowed_countries=[DE] → MATCH (Europe covers DE)
    de_cfg = _filters(location=LocationFilterConfig(allowed_countries=["DE"], allowed_cities=[], allow_unknown=True))
    assert filter_location(_post(location_tags=frozenset({"remote", "region:europe"})), de_cfg) is Verdict.MATCH


def test_location_unknown_remote_no_geo():
    # Remote with no geo signal → UNKNOWN
    assert filter_location(_post(location_tags=frozenset({"remote"})), _filters()) is Verdict.UNKNOWN


def test_location_match_city_seattle():
    # Hybrid in Seattle (allowed city) → MATCH
    assert filter_location(_post(location_tags=frozenset({"hybrid", "seattle"})), _filters()) is Verdict.MATCH


def test_location_city_match_with_conflicting_country_rejected():
    # "San Jose, Costa Rica" style: allowed-city name inside a foreign locale
    cfg = _filters(location=LocationFilterConfig(allowed_countries=["US"], allowed_cities=["san jose"], allow_unknown=True))
    assert filter_location(_post(location_tags=frozenset({"onsite_only", "san jose", "country:cr"})), cfg) is Verdict.REJECT


def test_location_reject_onsite_no_city():
    assert filter_location(_post(location_tags=frozenset({"onsite_only"})), _filters()) is Verdict.REJECT


def test_location_unknown_allowed():
    assert filter_location(_post(location_tags=frozenset({"unknown_location"})), _filters()) is Verdict.UNKNOWN


def test_location_unknown_rejected_when_not_allowed():
    strict_cfg = _filters(location=LocationFilterConfig(allowed_countries=["US"], allowed_cities=[], allow_unknown=False))
    assert filter_location(_post(location_tags=frozenset({"unknown_location"})), strict_cfg) is Verdict.REJECT


def test_location_match_remote_anywhere_policy():
    lenient_cfg = _filters(location=LocationFilterConfig(remote_policy="anywhere", allowed_cities=[], allow_unknown=True))
    assert filter_location(_post(location_tags=frozenset({"remote"})), lenient_cfg) is Verdict.MATCH


def test_location_legacy_remote_must_be_us_false_still_lenient():
    # Deprecation shim end-to-end: legacy False behaves like remote_policy=anywhere
    with pytest.warns(DeprecationWarning):
        legacy = LocationFilterConfig(remote_must_be_us=False, allowed_cities=[], allow_unknown=True)
    lenient_cfg = _filters(location=legacy)
    assert filter_location(_post(location_tags=frozenset({"remote"})), lenient_cfg) is Verdict.MATCH


# ---- stack ----
def test_stack_match_any_of():
    assert filter_stack(_post(stack=frozenset({"python"})), _filters()) is Verdict.MATCH


def test_stack_reject_no_overlap():
    assert filter_stack(_post(stack=frozenset({"java", "kotlin"})), _filters()) is Verdict.REJECT


def test_stack_unknown_when_empty():
    assert filter_stack(_post(stack=frozenset()), _filters()) is Verdict.UNKNOWN


# ---- comp ----
def test_comp_match_above_floor():
    assert filter_comp(_post(comp_min=150_000), _filters()) is Verdict.MATCH


def test_comp_reject_below_floor():
    assert filter_comp(_post(comp_min=80_000), _filters()) is Verdict.REJECT


def test_comp_unknown_when_missing():
    assert filter_comp(_post(comp_min=None), _filters()) is Verdict.UNKNOWN


# ---- age ----
NOW = datetime(2026, 5, 2, 12, 0, 0, tzinfo=timezone.utc)


@freeze_time(NOW)
def test_age_match_recent():
    f = _filters(max_age_days=14)
    assert filter_age(_post(posted_at=NOW - timedelta(days=3)), f) is Verdict.MATCH


@freeze_time(NOW)
def test_age_match_at_boundary():
    f = _filters(max_age_days=14)
    assert filter_age(_post(posted_at=NOW - timedelta(days=14)), f) is Verdict.MATCH


@freeze_time(NOW)
def test_age_reject_too_old():
    f = _filters(max_age_days=14)
    assert filter_age(_post(posted_at=NOW - timedelta(days=30)), f) is Verdict.REJECT


@freeze_time(NOW)
def test_age_reject_thousand_days_old():
    f = _filters(max_age_days=14)
    assert filter_age(_post(posted_at=NOW - timedelta(days=1000)), f) is Verdict.REJECT


def test_age_unknown_when_missing():
    f = _filters(max_age_days=14)
    assert filter_age(_post(posted_at=None), f) is Verdict.UNKNOWN


def test_age_match_when_unbounded():
    f = _filters(max_age_days=None)
    assert filter_age(_post(posted_at=datetime(2000, 1, 1, tzinfo=timezone.utc)), f) is Verdict.MATCH


@freeze_time(NOW)
def test_age_handles_naive_datetime():
    f = _filters(max_age_days=14)
    naive = (NOW - timedelta(days=3)).replace(tzinfo=None)
    assert filter_age(_post(posted_at=naive), f) is Verdict.MATCH


@freeze_time(NOW)
def test_evaluate_short_circuits_on_age_first():
    f = _filters(max_age_days=14)
    p = _post(posted_at=NOW - timedelta(days=1000), title="Office Manager")
    decision = evaluate(p, f)
    assert not decision.allow
    assert decision.rejected_by == "age"  # age runs before role in the pipeline


# ---- pipeline ----
def test_evaluate_short_circuits_on_role_reject():
    p = _post(title="Office Manager", stack=frozenset(), comp_min=None)
    decision = evaluate(p, _filters())
    assert not decision.allow
    assert decision.rejected_by == "role"


def test_evaluate_passes_with_unknowns():
    p = _post(stack=frozenset(), comp_min=None, location_tags=frozenset({"unknown_location"}))
    decision = evaluate(p, _filters())
    assert decision.allow
    assert "stack" in decision.unknowns
    assert "comp" in decision.unknowns
    assert "location" in decision.unknowns


def test_role_regex_catches_audit_evidenced_variants():
    """Phase 0 guard: _build_role_regex's matching semantics must catch the
    SWE-adjacent title variants the /audit role-gate evidence surfaced
    (2026-07-02), and keep excluding the deliberately-noisy ones, given a
    representative titles list. This no longer tests a shipped file —
    config.yaml is personal and untracked since the 2026-07-14
    untracked-personal-config change, so the fixture below is an inline
    snapshot of the titles list that was live on 2026-07-02 (git show
    60d7c80:config.yaml)."""
    from src.filters import _build_role_regex

    titles = [
        "software engineer",
        "software developer",
        "backend engineer",
        "backend developer",
        "frontend engineer",
        "frontend developer",
        "front-end engineer",
        "front-end developer",
        "front end engineer",
        "front end developer",
        "fullstack engineer",
        "full stack engineer",
        "full-stack engineer",
        "fullstack developer",
        "full stack developer",
        "full-stack developer",
        "c# developer",
        "c# engineer",
        "product engineer",
        "platform engineer",
        "devtools engineer",
        "developer experience engineer",
        "software development engineer",
        "sde",
        "member of technical staff",
        "smts",
        "lmts",
        "java developer",
        "java engineer",
        "python developer",
        "python engineer",
        "web developer",
        "web engineer",
        "application engineer",
        "application developer",
        "applications engineer",
        "cloud engineer",
        "infrastructure engineer",
    ]
    pat = _build_role_regex(tuple(titles))

    should_match = [
        "Sr. Software Development Engineer - Distributed Systems",
        "SDE II",
        "Member of Technical Staff",
        "Software Engineering SMTS, Salesforce",
        "Java Developer - Tech Lead",
        "Senior Python Engineer",
        "Staff Web Engineer",
        "Applications Engineer 2",
        "Senior Cloud Engineer",
        "Infrastructure Engineer",
    ]
    should_not_match = [
        "Sr. Solutions Engineer",
        "Automotive HVAC Systems Engineer",
        "Senior Manager, Software Engineering - JAX",
        "Office Manager, Berlin",
        "Growth Marketing Lead, Guest Engagement",
        # staff+ seniority is deliberately excluded (seniority_allow: [mid, senior])
        "Staff Engineer - Agent Marketplace",
        "Principal Engineer",
    ]
    for t in should_match:
        assert pat.search(t), f"should match: {t}"
    for t in should_not_match:
        assert not pat.search(t), f"should NOT match: {t}"
