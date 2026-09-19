from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from src.config import AppConfig

CONFIG_FIXTURE = {
    "filters": {
        "titles": ["software engineer"],
        "seniority_allow": ["mid", "senior"],
        "location": {"remote_must_be_us": True, "allowed_cities": ["dallas", "seattle", "denver"], "allow_unknown": True},
        "comp_floor_usd": 120000,
        "stack_any_of": ["python", "go"],
    },
    "quiet_hours": {
        "timezone": "America/Los_Angeles",
        "start": "23:00",
        "end": "07:00",
    },
    "sources": {
        "greenhouse": ["stripe"],
        "lever": [],
        "ashby": [],
        "workable": [],
        "hn_who_is_hiring": {"enabled": True},
    },
    "schedules": {"ats_minutes": 2, "slow_minutes": 15},
}

REPO_ROOT = Path(__file__).resolve().parent.parent


def _validate(doc: dict | str) -> AppConfig:
    """Validate a settings document (a dict or YAML text)."""
    return AppConfig.model_validate(yaml.safe_load(doc) if isinstance(doc, str) else doc)


def test_app_config_parses_the_fixture():
    cfg = _validate(CONFIG_FIXTURE)
    assert isinstance(cfg, AppConfig)
    assert cfg.filters.comp_floor_usd == 120_000
    assert cfg.sources.greenhouse == ["stripe"]
    assert cfg.sources.hn_who_is_hiring.enabled is True


def test_app_config_rejects_invalid_values():
    with pytest.raises(ValidationError):
        _validate({**CONFIG_FIXTURE, "schedules": {"ats_minutes": 0}})


def test_app_config_quiet_hours_parses_times():
    cfg = _validate(CONFIG_FIXTURE)
    assert cfg.quiet_hours.start.hour == 23
    assert cfg.quiet_hours.end.hour == 7
    assert str(cfg.quiet_hours.timezone) == "America/Los_Angeles"


def test_quiet_hours_config_rejects_unknown_timezone():
    from pydantic import ValidationError

    from src.config import QuietHoursConfig
    with pytest.raises(ValidationError, match="unknown timezone"):
        QuietHoursConfig(timezone="Mars/Olympus_Mons", start="22:00", end="07:00")


def test_sources_config_accepts_smartrecruiters_list():
    from src.config import SourcesConfig
    s = SourcesConfig(smartrecruiters=["foo", "bar"])
    assert s.smartrecruiters == ["foo", "bar"]


def test_sources_config_smartrecruiters_defaults_empty():
    from src.config import SourcesConfig
    s = SourcesConfig()
    assert s.smartrecruiters == []


def test_sources_config_workday_defaults_empty():
    from src.config import SourcesConfig
    s = SourcesConfig()
    assert s.workday == []


def test_sources_config_accepts_workday_tenants():
    from src.config import SourcesConfig, WorkdayTenant
    s = SourcesConfig(workday=[
        {"tenant": "microsoft", "region": "wd1", "site": "External"},
        {"tenant": "salesforce", "region": "wd12", "site": "External_Career_Site"},
    ])
    assert len(s.workday) == 2
    assert isinstance(s.workday[0], WorkdayTenant)
    assert s.workday[0].tenant == "microsoft"
    assert s.workday[0].region == "wd1"
    assert s.workday[0].site == "External"
    assert s.workday[1].site == "External_Career_Site"


def test_app_config_loads_discovery_section():
    cfg = _validate(
        """
filters:
  titles: ["software engineer"]
  seniority_allow: ["mid", "senior"]
  location:
    remote_must_be_us: true
    allowed_cities: []
    allow_unknown: true
  comp_floor_usd: 120000
  stack_any_of: ["python"]
quiet_hours:
  timezone: "UTC"
  start: "23:00"
  end: "07:00"
sources:
  greenhouse: []
  hiringcafe:
    enabled: true
    max_postings_per_cycle: 250
discovery:
  enabled: true
  max_validations_per_run: 75
  revalidate_after_days: 7
  quarantine_after_failures: 5
schedules:
  ats_minutes: 1
  slow_minutes: 15
  discovery_hours: 24
        """
    )

    assert cfg.discovery.enabled is True
    assert cfg.discovery.max_validations_per_run == 75
    assert cfg.discovery.quarantine_after_failures == 5
    assert cfg.sources.hiringcafe.enabled is True
    assert cfg.sources.hiringcafe.max_postings_per_cycle == 250
    assert cfg.schedules.discovery_hours == 24


def test_app_config_loads_extended_discovery_section():
    cfg = _validate(
        """
filters:
  titles: ["software engineer"]
  seniority_allow: ["mid", "senior"]
  location:
    remote_must_be_us: true
    allowed_cities: []
    allow_unknown: true
  comp_floor_usd: 120000
  stack_any_of: ["python"]
quiet_hours:
  timezone: "UTC"
  start: "23:00"
  end: "07:00"
sources:
  greenhouse: []
discovery:
  enabled: true
  max_validations_per_run: 500
  revalidate_after_days: 7
  no_match_revalidate_after_days: 90
  quarantine_after_failures: 5
  yc_oss_enabled: true
  yc_oss_min_team_size: 25
  manual_companies:
    - anthropic
    - cohere
schedules:
  ats_minutes: 1
  slow_minutes: 15
  discovery_hours: 24
        """
    )

    assert cfg.discovery.yc_oss_enabled is True
    assert cfg.discovery.yc_oss_min_team_size == 25
    assert cfg.discovery.no_match_revalidate_after_days == 90
    assert cfg.discovery.manual_companies == ["anthropic", "cohere"]


def test_app_config_discovery_defaults_when_new_fields_absent():
    """Backwards-compat: a config without the new fields uses sensible defaults."""
    cfg = _validate(
        """
filters:
  titles: ["software engineer"]
  seniority_allow: ["mid", "senior"]
  location:
    remote_must_be_us: true
    allowed_cities: []
    allow_unknown: true
  comp_floor_usd: 120000
  stack_any_of: ["python"]
quiet_hours:
  timezone: "UTC"
  start: "23:00"
  end: "07:00"
sources:
  greenhouse: []
discovery:
  enabled: true
  max_validations_per_run: 200
  revalidate_after_days: 7
  quarantine_after_failures: 5
schedules:
  ats_minutes: 1
  slow_minutes: 15
  discovery_hours: 24
        """
    )

    assert cfg.discovery.yc_oss_enabled is True   # default True
    assert cfg.discovery.yc_oss_min_team_size == 10  # default 10
    assert cfg.discovery.no_match_revalidate_after_days == 21  # default 21
    assert cfg.discovery.manual_companies == []


def test_app_config_loads_relevance_section():
    cfg = _validate(
        """
filters:
  titles: ["software engineer"]
  seniority_allow: ["mid", "senior"]
  location:
    remote_must_be_us: true
    allowed_cities: []
    allow_unknown: true
  comp_floor_usd: 120000
  stack_any_of: ["python"]
quiet_hours:
  timezone: "UTC"
  start: "23:00"
  end: "07:00"
sources:
  greenhouse: []
relevance:
  enabled: true
  provider: "anthropic"
  model: "claude-haiku-4-5"
  score_high: 7
  score_low: 3
  timeout_seconds: 10
schedules:
  ats_minutes: 1
  slow_minutes: 15
  discovery_hours: 24
        """
    )

    assert cfg.relevance.enabled is True
    assert cfg.relevance.model == "claude-haiku-4-5"
    assert cfg.relevance.score_high == 7
    assert cfg.relevance.score_low == 3
    assert cfg.relevance.timeout_seconds == 10


def test_app_config_relevance_defaults_when_section_absent():
    """Config without a relevance section uses sensible defaults."""
    cfg = _validate(
        """
filters:
  titles: ["software engineer"]
  seniority_allow: ["mid", "senior"]
  location:
    remote_must_be_us: true
    allowed_cities: []
    allow_unknown: true
  comp_floor_usd: 120000
  stack_any_of: ["python"]
quiet_hours:
  timezone: "UTC"
  start: "23:00"
  end: "07:00"
sources:
  greenhouse: []
schedules:
  ats_minutes: 1
  slow_minutes: 15
  discovery_hours: 24
        """
    )

    # Scoring stays opt-in (the wizard's LLM step pre-ticks the box instead —
    # a true default would make _llm_done() skip that step), but when it is
    # switched on it points at a local Ollama: the only provider that needs no
    # API key, so the shipped default can actually work out of the box.
    assert cfg.relevance.enabled is False
    assert cfg.relevance.provider == "ollama"
    # llama3.1:8b, not a gpt-oss size: the default ollama_host is the bundled
    # LOCAL service, and this is the exact model GETTING_STARTED's fully-local
    # recipe pulls, so the shipped default matches what a user already has.
    assert cfg.relevance.model == "llama3.1:8b"
    assert cfg.relevance.score_high == 7
    assert cfg.relevance.score_low == 3


def test_app_config_relevance_provider_defaults_to_ollama():
    """relevance.provider unspecified → 'ollama', the one provider that needs
    no API key and so can work on a fresh install without any signup."""
    cfg = _validate(
        """
filters:
  titles: ["software engineer"]
  seniority_allow: ["mid", "senior"]
  location: {remote_must_be_us: true, allowed_cities: [], allow_unknown: true}
  comp_floor_usd: 120000
  stack_any_of: ["python"]
quiet_hours: {timezone: "UTC", start: "23:00", end: "07:00"}
sources: {greenhouse: []}
schedules: {ats_minutes: 1, slow_minutes: 15, discovery_hours: 24}
        """
    )
    assert cfg.relevance.provider == "ollama"


def test_app_config_accepts_provider_gemini():
    """provider='gemini' is a valid value."""
    cfg = _validate(
        """
filters:
  titles: ["software engineer"]
  seniority_allow: ["mid", "senior"]
  location: {remote_must_be_us: true, allowed_cities: [], allow_unknown: true}
  comp_floor_usd: 120000
  stack_any_of: ["python"]
quiet_hours: {timezone: "UTC", start: "23:00", end: "07:00"}
sources: {greenhouse: []}
relevance:
  enabled: true
  provider: "gemini"
  model: "gemini-2.0-flash"
  score_high: 7
  score_low: 3
  timeout_seconds: 10
schedules: {ats_minutes: 1, slow_minutes: 15, discovery_hours: 24}
        """
    )
    assert cfg.relevance.provider == "gemini"
    assert cfg.relevance.model == "gemini-2.0-flash"


def test_app_config_google_api_key_defaults_to_empty():
    """secrets.google_api_key defaults to '' (secret resolution is covered in
    tests/settings/test_service.py)."""
    cfg = _validate(
        """
filters:
  titles: ["software engineer"]
  seniority_allow: ["mid", "senior"]
  location: {remote_must_be_us: true, allowed_cities: [], allow_unknown: true}
  comp_floor_usd: 120000
  stack_any_of: ["python"]
quiet_hours: {timezone: "UTC", start: "23:00", end: "07:00"}
sources: {greenhouse: []}
schedules: {ats_minutes: 1, slow_minutes: 15, discovery_hours: 24}
        """
    )
    assert cfg.secrets.google_api_key == ""


def test_app_config_accepts_provider_ollama():
    """provider='ollama' is a valid value."""
    cfg = _validate(
        """
filters:
  titles: ["software engineer"]
  seniority_allow: ["mid", "senior"]
  location: {remote_must_be_us: true, allowed_cities: [], allow_unknown: true}
  comp_floor_usd: 120000
  stack_any_of: ["python"]
quiet_hours: {timezone: "UTC", start: "23:00", end: "07:00"}
sources: {greenhouse: []}
relevance:
  enabled: true
  provider: "ollama"
  model: "gpt-oss:20b"
  score_high: 7
  score_low: 4
  timeout_seconds: 10
schedules: {ats_minutes: 1, slow_minutes: 15, discovery_hours: 24}
        """
    )
    assert cfg.relevance.provider == "ollama"
    assert cfg.relevance.model == "gpt-oss:20b"


def test_app_config_ollama_api_key_defaults_to_empty():
    """secrets.ollama_api_key defaults to '' (secret resolution is covered in
    tests/settings/test_service.py)."""
    cfg = _validate(
        """
filters:
  titles: ["software engineer"]
  seniority_allow: ["mid", "senior"]
  location: {remote_must_be_us: true, allowed_cities: [], allow_unknown: true}
  comp_floor_usd: 120000
  stack_any_of: ["python"]
quiet_hours: {timezone: "UTC", start: "23:00", end: "07:00"}
sources: {greenhouse: []}
schedules: {ats_minutes: 1, slow_minutes: 15, discovery_hours: 24}
        """
    )
    assert cfg.secrets.ollama_api_key == ""


def test_app_config_rejects_unknown_provider():
    """provider='openai' (or anything not anthropic/gemini/ollama) fails validation."""
    with pytest.raises(ValidationError):
        _validate(
            """
filters:
  titles: ["software engineer"]
  seniority_allow: ["mid", "senior"]
  location: {remote_must_be_us: true, allowed_cities: [], allow_unknown: true}
  comp_floor_usd: 120000
  stack_any_of: ["python"]
quiet_hours: {timezone: "UTC", start: "23:00", end: "07:00"}
sources: {greenhouse: []}
relevance: {enabled: true, provider: "openai", model: "gpt-4o-mini"}
schedules: {ats_minutes: 1, slow_minutes: 15, discovery_hours: 24}
            """
        )


def test_gap_analysis_defaults_disabled():
    from src.config import GapAnalysisConfig
    cfg = GapAnalysisConfig()
    assert cfg.enabled is False
    assert cfg.provider is None          # None → reuse relevance.provider
    assert cfg.model is None
    assert cfg.timeout_seconds == 20
    assert cfg.max_skills_per_job == 6
    assert cfg.digest_window_days == 30


def test_app_config_has_gap_analysis_default():
    """AppConfig without a gap_analysis block defaults to a disabled feature."""
    cfg = _validate(
        """
filters:
  titles: ["software engineer"]
  seniority_allow: ["senior"]
  location: {remote_must_be_us: true, allowed_cities: [], allow_unknown: true}
  comp_floor_usd: 120000
  stack_any_of: ["python"]
quiet_hours: {timezone: "UTC", start: "23:00", end: "07:00"}
sources: {greenhouse: []}
schedules: {ats_minutes: 2, slow_minutes: 15}
"""
    )
    assert cfg.gap_analysis.enabled is False


def test_sources_rippling_defaults_empty_and_parses_list(tmp_path, monkeypatch):
    from src.config import SourcesConfig
    # default is an empty list
    assert SourcesConfig().rippling == []
    # parses a provided list
    assert SourcesConfig(rippling=["acme", "globex"]).rippling == ["acme", "globex"]


def test_tailoring_config_defaults():
    from src.config import TailoringConfig
    c = TailoringConfig()
    assert c.enabled is False
    assert c.provider is None          # defaults to relevance provider at build time
    assert c.model is None
    assert c.timeout_seconds == 60     # tailoring is a long call; relevance's 20s is too tight


def test_appconfig_has_tailoring_default():
    from src.config import AppConfig
    assert "tailoring" in AppConfig.model_fields


def test_secrets_have_tailor_endpoint_fields():
    from src.config import Secrets
    s = Secrets(ntfy_topic_url="x", discord_webhook_url="x")
    assert s.tailor_endpoint_url == "" and s.tailor_signing_secret == ""


def test_board_config_defaults_to_ten():
    from src.config import BoardConfig
    assert BoardConfig().stale_after_days == 10


def test_board_config_accepts_override():
    from src.config import BoardConfig
    assert BoardConfig(stale_after_days=21).stale_after_days == 21


def test_app_config_board_defaults_when_absent():
    cfg = _validate(CONFIG_FIXTURE)
    assert cfg.board.stale_after_days == 10


def test_audit_config_defaults():
    from src.config import AuditConfig
    a = AuditConfig()
    assert a.enabled is True
    assert a.retention_days == 90


def test_ops_notify_config_defaults():
    from src.config import OpsNotifyConfig
    o = OpsNotifyConfig()
    assert o.llm_degraded_cycles == 2
    assert o.zero_yield_hours == 12
    assert o.cooldown_hours == 6


def test_app_config_gains_audit_and_ops_notify_defaults():
    # minimal valid document without audit/ops_notify blocks -> defaults apply
    cfg = _validate(
        "filters:\n"
        "  titles: [software engineer]\n"
        "  seniority_allow: [mid]\n"
        "  location: {remote_must_be_us: true}\n"
        "  comp_floor_usd: 0\n"
        "  stack_any_of: [python]\n"
        "quiet_hours: {timezone: UTC, start: '23:00', end: '07:00'}\n"
        "sources: {}\n"
        "schedules: {ats_minutes: 2, slow_minutes: 15}\n"
    )
    assert cfg.audit.retention_days == 90
    assert cfg.ops_notify.cooldown_hours == 6


def test_sources_oraclecloud_parses_tenant_triples():
    from src.config import OracleCloudTenant, SourcesConfig
    src = SourcesConfig(oraclecloud=[
        {"tenant": "egug", "region": "us2", "site": "CX_1"},
    ])
    assert src.oraclecloud == [OracleCloudTenant(tenant="egug", region="us2", site="CX_1")]
    assert src.oraclecloud[0].company is None  # optional, defaults unset
    assert SourcesConfig().oraclecloud == []  # optional, defaults empty


def test_sources_oraclecloud_parses_optional_company_display_name():
    from src.config import OracleCloudTenant, SourcesConfig
    src = SourcesConfig(oraclecloud=[
        {"tenant": "egug", "region": "us2", "site": "CX_1", "company": "American Express"},
    ])
    assert src.oraclecloud == [
        OracleCloudTenant(tenant="egug", region="us2", site="CX_1", company="American Express")
    ]


def test_board_config_automation_defaults():
    from src.config import BoardConfig
    b = BoardConfig()
    assert b.stale_after_days == 10
    assert b.closed_check_cron == "30 4 * * *"
    assert b.digest_cron == "0 15 * * *"
    assert b.web_base_url == ""


def test_gmail_config_defaults():
    from src.config import GmailConfig
    cfg = GmailConfig()
    assert cfg.check_cron == "0 * * * *"
    assert cfg.first_run_days == 3
    assert cfg.lookback_max_days == 7
    assert cfg.max_messages_per_run == 200


def test_gmail_config_accepts_override():
    from src.config import GmailConfig
    assert GmailConfig(check_cron="30 */2 * * *").check_cron == "30 */2 * * *"


def test_jsonld_board_defaults_company_none():
    from src.config import JsonLdBoard
    b = JsonLdBoard(family="icims", slug="steeldynamics",
                    base_url="https://careers-steeldynamics.icims.com")
    assert b.company is None
    assert b.family == "icims"


def test_jsonld_board_rejects_unknown_family():
    import pytest
    from pydantic import ValidationError
    from src.config import JsonLdBoard
    with pytest.raises(ValidationError):
        JsonLdBoard(family="workday", slug="x", base_url="https://x")


def test_eightfold_tenant_defaults_flavor_pcsx():
    from src.config import EightfoldTenant
    t = EightfoldTenant(slug="bms", domain="bms.com")
    assert t.flavor == "pcsx"
    assert t.company is None


def test_eightfold_tenant_rejects_unknown_flavor():
    import pytest
    from pydantic import ValidationError
    from src.config import EightfoldTenant
    with pytest.raises(ValidationError):
        EightfoldTenant(slug="bms", domain="bms.com", flavor="smartapply")


def test_phenom_board_defaults_company_none():
    from src.config import PhenomBoard
    b = PhenomBoard(careers_url="https://careers.fisglobal.com")
    assert b.company is None
    assert b.careers_url == "https://careers.fisglobal.com"


def test_taleo_board_defaults_company_none():
    from src.config import TaleoBoard
    b = TaleoBoard(tenant="cinfin", section="ex")
    assert b.company is None
    assert b.tenant == "cinfin" and b.section == "ex"


def test_discovery_board_knobs_default():
    from src.config import DiscoveryConfig
    d = DiscoveryConfig()
    assert d.board_discovery_enabled is True
    assert d.board_max_sweeps_per_run == 60
    assert d.board_revalidate_after_days == 14


def test_discovery_config_candidate_knob_defaults(tmp_path):
    from src.config import DiscoveryConfig
    d = DiscoveryConfig()
    assert d.hiringcafe_mining_enabled is True
    assert d.candidate_capture_cap == 50
    assert d.revalidate_reserve == 100


def test_discovery_vc_knobs_default():
    from src.config import DiscoveryConfig
    d = DiscoveryConfig()
    assert d.vc_firms == []
    assert d.vc_refresh_days == 7
    assert d.vc_capture_cap == 500


def test_discovery_vc_knobs_accept_override():
    from src.config import DiscoveryConfig
    d = DiscoveryConfig(vc_firms=["a16z", "sequoia"], vc_refresh_days=14,
                        vc_capture_cap=100)
    assert d.vc_firms == ["a16z", "sequoia"]
    assert d.vc_refresh_days == 14
    assert d.vc_capture_cap == 100


def test_avature_board_and_headless_schedule():
    from src.config import AvatureBoard, SchedulesConfig
    b = AvatureBoard(careers_url="https://careers.jacobs.com/en_US/careers/SearchJobs", company="Jacobs")
    assert b.company == "Jacobs"
    assert SchedulesConfig(ats_minutes=1, slow_minutes=1).headless_minutes == 45


# ---- configurable geography (LocationFilterConfig) ----
from pydantic import ValidationError as _ValidationError

from src.config import LocationFilterConfig as _Loc


def test_location_defaults_to_us():
    loc = _Loc()
    assert loc.allowed_countries == ["US"]
    assert loc.remote_policy == "allowed_countries"


def test_location_legacy_true_translates_to_allowed_countries():
    with pytest.warns(DeprecationWarning):
        loc = _Loc(remote_must_be_us=True)
    assert loc.remote_policy == "allowed_countries"
    assert loc.allowed_countries == ["US"]


def test_location_legacy_false_translates_to_anywhere():
    with pytest.warns(DeprecationWarning):
        loc = _Loc(remote_must_be_us=False)
    assert loc.remote_policy == "anywhere"


def test_location_legacy_plus_new_key_is_an_error():
    with pytest.raises(_ValidationError, match="not both"):
        _Loc(remote_must_be_us=True, remote_policy="anywhere")


def test_location_country_codes_canonicalized():
    loc = _Loc(allowed_countries=["uk", "de", "us"])
    assert loc.allowed_countries == ["GB", "DE", "US"]


def test_location_unknown_country_code_rejected():
    with pytest.raises(_ValidationError, match="XX"):
        _Loc(allowed_countries=["XX"])


def test_location_empty_allowed_countries_rejected():
    with pytest.raises(_ValidationError, match="must not be empty"):
        _Loc(allowed_countries=[])


def test_location_legacy_shim_does_not_mutate_callers_dict():
    """The mode=before shim must copy its input: validating the same dict
    twice must succeed both times and leave the caller's dict untouched."""
    shared = {"remote_must_be_us": True}
    with pytest.warns(DeprecationWarning):
        first = _Loc.model_validate(shared)
    with pytest.warns(DeprecationWarning):
        second = _Loc.model_validate(shared)
    assert first.remote_policy == "allowed_countries"
    assert second.remote_policy == "allowed_countries"
    assert "remote_policy" not in shared  # caller's dict unmutated


def test_location_legacy_instance_revalidates_inside_filters_config():
    """A pre-built legacy instance embedded in FiltersConfig must re-validate
    without a false 'not both' conflict (the nested-instance scenario that
    requires the shim to run in mode=before)."""
    from src.config import FiltersConfig
    with pytest.warns(DeprecationWarning):
        loc = _Loc(remote_must_be_us=True)
    filters = FiltersConfig(
        titles=["software engineer"],
        seniority_allow=["senior"],
        location=loc,
        comp_floor_usd=120000,
        stack_any_of=["python"],
    )
    assert filters.location.remote_policy == "allowed_countries"


def test_sources_eu_families_default_empty():
    from src.config import SourcesConfig
    s = SourcesConfig()
    assert s.personio == [] and s.recruitee == [] and s.teamtailor == []


def test_hiringcafe_extra_queries_defaults_empty():
    from src.config import HiringCafeConfig
    assert HiringCafeConfig().extra_queries == []


def test_discovery_eu_seeds_disabled_by_default():
    from src.config import DiscoveryConfig as _Disc
    assert _Disc().eu_seeds_enabled is False


def test_hiringcafe_location_defaults_none():
    from src.config import HiringCafeConfig
    assert HiringCafeConfig().location is None


def test_hiringcafe_extra_queries_accepts_strings_and_mappings():
    from src.config import HiringCafeConfig, HiringCafeSearch
    cfg = HiringCafeConfig(extra_queries=[
        "staff platform engineer",
        {"query": "software engineer", "location": "Europe"},
    ])
    assert cfg.extra_queries[0] == "staff platform engineer"
    assert isinstance(cfg.extra_queries[1], HiringCafeSearch)
    assert cfg.extra_queries[1].query == "software engineer"
    assert cfg.extra_queries[1].location == "Europe"


def test_hiringcafe_location_normalizes_countries_and_continents():
    from src.config import HiringCafeConfig, HiringCafeSearch
    assert HiringCafeConfig(location="us").location == "US"
    assert HiringCafeConfig(location="UK").location == "GB"  # alias
    assert HiringCafeSearch(query="q", location="europe").location == "Europe"
    assert HiringCafeSearch(query="q", location="north america").location == "North America"


def test_hiringcafe_location_rejects_unknown_values():
    import pytest as _pytest
    from src.config import HiringCafeConfig, HiringCafeSearch
    with _pytest.raises(ValueError, match="Atlantis"):
        HiringCafeConfig(location="Atlantis")
    # unknown 2-letter code takes the country path but must still fail loudly
    with _pytest.raises(ValueError, match="continent"):
        HiringCafeSearch(query="q", location="XX")


def test_example_config_loads():
    cfg = _validate((REPO_ROOT / "config.example.yaml").read_text())
    assert cfg.sources.greenhouse  # starter slugs present


def test_adzuna_defaults_are_inert():
    from src.config import AdzunaConfig

    a = AdzunaConfig()
    assert a.enabled is False
    assert a.countries == ["us"]
    assert a.queries == []
    assert a.max_days_old == 2
    assert a.results_per_page == 50
    assert a.daily_call_budget == 60


def test_adzuna_enabled_requires_queries():
    import pytest
    from pydantic import ValidationError
    from src.config import AdzunaConfig

    with pytest.raises(ValidationError, match="queries"):
        AdzunaConfig(enabled=True, queries=[])


def test_adzuna_countries_normalized_and_validated():
    import pytest
    from pydantic import ValidationError
    from src.config import AdzunaConfig

    a = AdzunaConfig(countries=[" US ", "gb"])
    assert a.countries == ["us", "gb"]
    with pytest.raises(ValidationError, match="alpha-2"):
        AdzunaConfig(countries=["usa"])
    with pytest.raises(ValidationError, match="must not be empty"):
        AdzunaConfig(countries=[])



# --- settings foundation (sub-project 1): every section has a default ---

def test_app_config_validates_empty_document():
    from src.config import AppConfig
    cfg = AppConfig.model_validate({})
    assert cfg.filters.titles == []
    assert cfg.filters.seniority_allow == ["mid", "senior"]
    assert cfg.filters.comp_floor_usd == 0
    assert cfg.filters.stack_any_of == []
    assert cfg.filters.location.allowed_countries == ["US"]
    assert cfg.quiet_hours is None
    assert cfg.sources.greenhouse == []
    assert cfg.schedules.ats_minutes == 10
    assert cfg.schedules.slow_minutes == 15
    assert cfg.secrets.ntfy_topic_url == ""
    assert cfg.secrets.discord_webhook_url == ""


def test_filters_location_annotation_is_resolved():
    """LocationFilterConfig is defined before FiltersConfig, so the field's
    annotation is the class itself (not a ForwardRef) — the settings import's
    unknown-key walker relies on this."""
    from src.config import FiltersConfig, LocationFilterConfig
    assert FiltersConfig.model_fields["location"].annotation is LocationFilterConfig


def test_relevance_ollama_host_default_and_locality():
    from src.config import RelevanceConfig
    assert RelevanceConfig().ollama_host == "http://ollama:11434"
    assert RelevanceConfig().ollama_is_local is True
    assert RelevanceConfig(ollama_host="https://ollama.com").ollama_is_local is False


def test_ollama_is_local_is_not_a_config_field():
    from src.config import RelevanceConfig
    assert "ollama_is_local" not in RelevanceConfig.model_fields


@pytest.mark.parametrize("section,field", [
    ("schedules", "digest_cron"),
    ("board", "closed_check_cron"),
    ("board", "digest_cron"),
    ("gmail", "check_cron"),
])
def test_cron_fields_reject_invalid_crontab(section, field):
    from pydantic import ValidationError
    from src.config import AppConfig
    with pytest.raises(ValidationError, match="invalid crontab"):
        AppConfig.model_validate({section: {field: "not a cron"}})


def test_cron_fields_accept_valid_crontab():
    from src.config import AppConfig
    cfg = AppConfig.model_validate({"schedules": {"digest_cron": "15 9 * * 2"}})
    assert cfg.schedules.digest_cron == "15 9 * * 2"


def test_location_legacy_key_does_not_survive_a_dump_round_trip():
    """The legacy shim must drop remote_must_be_us, or a dumped document
    carries both keys and fails re-validation with 'not both'."""
    from src.config import LocationFilterConfig
    with pytest.warns(DeprecationWarning):
        loc = LocationFilterConfig.model_validate({"remote_must_be_us": False})
    dumped = loc.model_dump(mode="json", exclude_defaults=True)
    assert dumped == {"remote_policy": "anywhere"}
    assert LocationFilterConfig.model_validate(dumped).remote_policy == "anywhere"


def test_slug_source_families_are_plain_slug_lists():
    from src.config import SLUG_SOURCE_FAMILIES, SourcesConfig
    for family in SLUG_SOURCE_FAMILIES:
        assert SourcesConfig.model_fields[family].annotation == list[str]


def test_document_path_flags_are_gone():
    from src.config import GapAnalysisConfig, RelevanceConfig, TailoringConfig
    assert "profile_path" not in RelevanceConfig.model_fields
    assert "resume_path" not in GapAnalysisConfig.model_fields
    assert {"content_path", "evidence_path"}.isdisjoint(TailoringConfig.model_fields)
    assert "kit" not in AppConfig.model_fields
