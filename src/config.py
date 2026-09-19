"""Settings models: the settings document (stored in SQLite and validated by
src/settings/service.py) plus the Secrets it resolves."""

from __future__ import annotations

import warnings
from datetime import time
from typing import Literal, get_args
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src import geo


EmploymentType = Literal[
    "full_time", "part_time", "contract", "contract_to_hire", "temporary", "internship"
]
# Derived, not hand-copied — a hand-written tuple would silently drift from
# the Literal above. Not a config flag itself, so it carries no docs/CONFIG.md
# row (tests/test_config_docs.py walks the model tree, not module constants).
EMPLOYMENT_TYPES: tuple[str, ...] = get_args(EmploymentType)


def _check_crontab(value: str) -> str:
    """Reject crontab strings APScheduler can't parse. The scheduler applies
    these live, so a bad value must fail at save time, not at reschedule."""
    from apscheduler.triggers.cron import CronTrigger
    try:
        CronTrigger.from_crontab(value, timezone="UTC")
    except ValueError as exc:
        raise ValueError(f"invalid crontab {value!r}: {exc}") from exc
    return value


class LocationFilterConfig(BaseModel):
    allowed_countries: list[str] = Field(default_factory=lambda: ["US"])
    allowed_cities: list[str] = Field(default_factory=list)
    remote_policy: Literal["allowed_countries", "anywhere"] = Field(default="allowed_countries")
    allow_unknown: bool = True
    # Deprecated pre-v0.6 key; translated into remote_policy below. Downstream
    # code must read remote_policy only.
    remote_must_be_us: bool | None = None

    @field_validator("allowed_countries")
    @classmethod
    def _canonicalize_countries(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("location.allowed_countries must not be empty")
        return [geo.validate_country_code(c) for c in v]

    @model_validator(mode="before")
    @classmethod
    def _translate_legacy_key(cls, data):
        if isinstance(data, dict):
            data = dict(data)  # never mutate the caller's dict
            # Popped, not just read: a dumped document must never carry the
            # legacy key alongside remote_policy (re-validation would fail).
            legacy = data.pop("remote_must_be_us", None)
            if legacy is not None:
                if "remote_policy" in data:
                    raise ValueError(
                        "location: set remote_policy or the deprecated "
                        "remote_must_be_us, not both"
                    )
                warnings.warn(
                    "location.remote_must_be_us is deprecated; use "
                    "remote_policy: allowed_countries|anywhere",
                    DeprecationWarning,
                    stacklevel=2,
                )
                data["remote_policy"] = "allowed_countries" if legacy else "anywhere"
        return data


class FiltersConfig(BaseModel):
    # An empty titles list matches nothing (see filters._build_role_regex) —
    # safe but silent; the settings UI surfaces it as a readiness warning.
    titles: list[str] = Field(default_factory=list)
    seniority_allow: list[Literal["junior", "mid", "senior", "staff"]] = Field(
        default_factory=lambda: ["mid", "senior"]
    )
    location: LocationFilterConfig = Field(default_factory=LocationFilterConfig)
    comp_floor_usd: int = Field(default=0, ge=0)
    stack_any_of: list[str] = Field(default_factory=list)
    max_age_days: int | None = None  # None = no age filter; otherwise reject postings older than N days
    # Employment types to hard-reject when a posting's type is *known*. Postings
    # with an unknown type (most connectors don't expose it) are never rejected
    # here — the filter fails open. "contract_to_hire" and "full_time" are
    # intentionally absent from the default (the profile accepts both).
    blocked_employment_types: list[EmploymentType] = Field(
        default_factory=lambda: ["contract", "temporary", "part_time", "internship"]
    )
    # Companies to hard-reject, whatever source surfaced them. Matched on
    # whole-word tokens rather than substrings, so "microsoft" also catches
    # "Microsoft Corporation" and the slug-derived "Eightfold:Microsoft" while
    # "apple" leaves "Applebee's" alone. Empty (the default) disables the gate.
    blocked_companies: list[str] = Field(default_factory=list)


class QuietHoursConfig(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    timezone: ZoneInfo
    start: time
    end: time

    @field_validator("timezone", mode="before")
    @classmethod
    def _coerce_tz(cls, v: object) -> ZoneInfo:
        if isinstance(v, ZoneInfo):
            return v
        try:
            return ZoneInfo(str(v))
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown timezone {v!r}") from exc

    @field_validator("start", "end", mode="before")
    @classmethod
    def _coerce_time(cls, v: object) -> time:
        if isinstance(v, time):
            return v
        return time.fromisoformat(str(v))


class AggregatorConfig(BaseModel):
    enabled: bool = True


# Backward-compatibility alias — existing imports and tests use HnConfig
HnConfig = AggregatorConfig


# Continent names hiring.cafe's searchState `locations` key recognizes
# (matched on formatted_address; canonical spellings, verified live 2026-07-11).
_HIRINGCAFE_CONTINENTS: dict[str, str] = {
    "europe": "Europe", "asia": "Asia", "north america": "North America",
    "south america": "South America", "africa": "Africa",
    "oceania": "Oceania", "antarctica": "Antarctica",
}


def _normalize_hiringcafe_location(raw: str) -> str:
    """Canonicalize a hiring.cafe search location: uppercase ISO-2 country
    code or a continent name in hiring.cafe's canonical spelling."""
    s = " ".join(raw.split())
    if len(s) == 2 and s.isalpha():
        try:
            return geo.validate_country_code(s)
        except ValueError:
            pass  # fall through to the unified error below
    canonical = _HIRINGCAFE_CONTINENTS.get(s.lower())
    if canonical is None:
        raise ValueError(
            f"unknown hiring.cafe location {raw!r} — use an ISO 3166-1 alpha-2 "
            "country code (US, DE, GB, ...) or a continent name (Europe, Asia, "
            "North America, South America, Africa, Oceania, Antarctica)"
        )
    return canonical


class HiringCafeSearch(BaseModel):
    """A geo-scoped hiring.cafe search: {query, location} mapping form of an
    extra_queries entry. location is an ISO-2 country code or continent name."""

    query: str
    location: str | None = None

    @field_validator("location")
    @classmethod
    def _check_location(cls, v: str | None) -> str | None:
        return None if v is None else _normalize_hiringcafe_location(v)


class HiringCafeConfig(BaseModel):
    enabled: bool = False
    max_postings_per_cycle: int = Field(default=500, ge=1)
    # Geographic scope for the primary (DEFAULT_QUERY) search — ISO-2 country
    # code or continent name. Unset, hiring.cafe geo-defaults results by the
    # server's view of the box's egress IP.
    location: str | None = None
    # Extra search queries run each cycle alongside the default; results are
    # deduped by posting id and share max_postings_per_cycle. Entries are
    # plain strings (keyword-only search) or {query, location} mappings for
    # geo-scoped searches.
    extra_queries: list[str | HiringCafeSearch] = Field(default_factory=list)

    @field_validator("location")
    @classmethod
    def _check_location(cls, v: str | None) -> str | None:
        return None if v is None else _normalize_hiringcafe_location(v)


class AdzunaConfig(BaseModel):
    """Adzuna job-search API aggregator (slow tier, opt-in). Free-tier quotas
    are 25/min, 250/day, 1,000/week, 2,500/month — daily_call_budget hard-stops
    each cycle's API calls well under the monthly ceiling. Requires the
    JOB_AGG_ADZUNA_APP_ID / JOB_AGG_ADZUNA_APP_KEY secrets; enabled without
    them, build_connectors logs an error and skips the connector."""

    enabled: bool = False
    countries: list[str] = Field(default_factory=lambda: ["us"])
    queries: list[str] = Field(default_factory=list)
    max_days_old: int = Field(default=2, ge=1)
    results_per_page: int = Field(default=50, ge=1, le=50)
    daily_call_budget: int = Field(default=60, ge=1)

    @field_validator("countries")
    @classmethod
    def _check_countries(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("sources.adzuna.countries must not be empty")
        out: list[str] = []
        for c in v:
            s = c.strip().lower()
            if len(s) != 2 or not s.isalpha():
                raise ValueError(
                    f"sources.adzuna.countries entries must be ISO 3166-1 alpha-2 codes, got {c!r}"
                )
            out.append(s)
        return out

    @model_validator(mode="after")
    def _require_queries_when_enabled(self) -> "AdzunaConfig":
        if self.enabled and not self.queries:
            raise ValueError(
                "sources.adzuna.enabled requires at least one entry in sources.adzuna.queries"
            )
        return self


class DiscoveryConfig(BaseModel):
    enabled: bool = False
    max_validations_per_run: int = Field(default=200, ge=1)
    revalidate_after_days: int = Field(default=7, ge=1)
    # Pairs with filters.max_age_days: a company that adopts an ATS mid-window
    # stays invisible until the next probe, and by then its whole backlog is
    # past the age gate and gets dropped — so a long cadence costs the FRESH
    # postings this app exists to catch, not just stale ones. 90 -> 21 (2026-07-27).
    no_match_revalidate_after_days: int = Field(default=21, ge=1)
    quarantine_after_failures: int = Field(default=5, ge=1)
    yc_oss_enabled: bool = True
    yc_oss_min_team_size: int = Field(default=10, ge=0)
    manual_companies: list[str] = Field(default_factory=list)
    board_discovery_enabled: bool = True
    board_max_sweeps_per_run: int = Field(default=60, ge=1)
    board_revalidate_after_days: int = Field(default=14, ge=1)
    board_quarantine_after_failures: int = Field(default=5, ge=1)
    eu_seeds_enabled: bool = False  # opt-in: append scripts/seeds/eu_companies.csv to the board sweep
    hiringcafe_mining_enabled: bool = True
    candidate_capture_cap: int = Field(default=50, ge=1)
    revalidate_reserve: int = Field(default=100, ge=0)
    vc_firms: list[str] = Field(default_factory=list)  # e.g. ["a16z", "sequoia"]; empty = inert
    vc_refresh_days: int = Field(default=7, ge=1)
    vc_capture_cap: int = Field(default=500, ge=1)     # per firm per run; staging is HTTP-free


class RelevanceConfig(BaseModel):
    # Scoring stays opt-in: web.wizard.steps._llm_done() reads
    # relevance.enabled as "has the user decided anything on the LLM step yet?",
    # so defaulting it true would mark that step complete on a fresh install and
    # skip the one screen where the provider, model and key get set. The wizard
    # pre-ticks the box instead (web.wizard.routes.llm_prefill).
    enabled: bool = False
    # ...but when it IS switched on, it points at Ollama: the only provider that
    # needs no API key, so the shipped default can work without a signup first.
    provider: Literal["anthropic", "gemini", "ollama"] = "ollama"
    model: str = "gpt-oss:120b"
    score_high: int = Field(default=7, ge=0, le=10)
    score_low: int = Field(default=3, ge=0, le=10)
    timeout_seconds: int = Field(default=10, ge=1)
    # Base URL for every Ollama-backed feature. "https://ollama.com" is hosted
    # Ollama Cloud (needs secrets.ollama_api_key); anything else is a local
    # server that needs no key. JOB_AGG_OLLAMA_HOST, when non-empty, overrides it.
    ollama_host: str = "http://ollama:11434"

    @property
    def ollama_is_local(self) -> bool:
        return "ollama.com" not in self.ollama_host


class GapAnalysisConfig(BaseModel):
    enabled: bool = False
    # provider/model default to the relevance values when None (see _build_gap_analyzer)
    provider: Literal["anthropic", "gemini", "ollama"] | None = None
    model: str | None = None
    timeout_seconds: int = Field(default=20, ge=1)
    max_skills_per_job: int = Field(default=6, ge=1)
    digest_window_days: int = Field(default=30, ge=1)


class ResumeDraftConfig(BaseModel):
    """The first-run wizard's résumé -> profile + filters drafting.

    No `enabled` flag on purpose: drafting is offered when a provider binding
    can be built and falls back to hand-filled forms when it cannot, so there
    is nothing for a flag to switch off."""
    provider: Literal["anthropic", "gemini", "ollama"] | None = None
    model: str | None = None
    timeout_seconds: int = 60


class TailoringConfig(BaseModel):
    enabled: bool = False
    # provider/model default to the relevance values when None (see build_tailor_engine)
    provider: Literal["anthropic", "gemini", "ollama"] | None = None
    model: str | None = None
    timeout_seconds: int = Field(default=60, ge=1)


class BoardConfig(BaseModel):
    stale_after_days: int = Field(default=10, ge=1)
    # Board automation (local scheduler only): daily posting-closed sweep and
    # the stale/closed digest. web_base_url, when set (e.g. a Tailscale URL),
    # becomes the digest's ntfy click-through to /board.
    closed_check_cron: str = "30 4 * * *"
    digest_cron: str = "0 15 * * *"
    web_base_url: str = ""

    @field_validator("closed_check_cron", "digest_cron")
    @classmethod
    def _check_cron(cls, v: str) -> str:
        return _check_crontab(v)


class HttpConfig(BaseModel):
    """Outbound HTTP identity.

    ``user_agent`` overrides the honest default (see src/user_agent.py). Some
    enterprise ATS front ends reject unfamiliar clients on the User-Agent
    alone; overriding is the operator's call, and the operator is responsible
    for whether the value they send is consistent with the target site's terms
    of use. Blank/omitted = the honest default."""

    user_agent: str | None = None


class AuditConfig(BaseModel):
    enabled: bool = True
    retention_days: int = Field(default=90, ge=1)


class CoachConfig(BaseModel):
    """The /coach page: on-demand LLM recommendations for improving application
    response rates, grounded in the user's own funnel/audit/config/résumé data.
    Run history persists in SQLite (like the audit trail)."""
    enabled: bool = True
    # provider/model default to the relevance values when None (see _build_coach)
    provider: Literal["anthropic", "gemini", "ollama"] | None = None
    model: str | None = None
    timeout_seconds: int = Field(default=120, ge=1)  # full-snapshot analysis is a long generation
    max_jobs: int = Field(default=100, ge=1)         # most-recent pursued jobs in the snapshot
    window_days: int = Field(default=90, ge=1)       # lookback for rejection-audit aggregates


class OpsNotifyConfig(BaseModel):
    """Thresholds for the ops-alert conditions. The ops sink URLs live in
    Secrets (env vars) like every other notification target; alerts are
    disabled unless at least one ops URL is set."""
    llm_degraded_cycles: int = Field(default=2, ge=1)
    zero_yield_hours: int = Field(default=12, ge=1)
    cooldown_hours: int = Field(default=6, ge=1)
    # Per-source watchdog (SQLite runtimes only — needs the rejected-postings
    # store). zero_yield_hours above is pipeline-wide and cannot see one source
    # dying while the rest work; these two govern the per-family condition that
    # can. Raise source_min_baseline_rows to watch fewer, busier families.
    source_zero_yield_hours: int = Field(default=24, ge=1)
    source_min_baseline_rows: int = Field(default=2000, ge=1)


class WorkdayTenant(BaseModel):
    """One Workday job-board endpoint. Workday companies are addressed by a
    (tenant, region, site) triple — e.g. microsoft.wd1.myworkdayjobs.com/External
    is `tenant=microsoft, region=wd1, site=External`. Two sites at the same
    tenant are distinct boards (External vs Internal vs Contractors)."""
    tenant: str
    region: str
    site: str


class OracleCloudTenant(BaseModel):
    """One Oracle Recruiting Cloud CandidateExperience board, addressed by a
    (tenant, region, site) triple — e.g. egug.fa.us2.oraclecloud.com with
    siteNumber CX_1 is `tenant=egug, region=us2, site=CX_1` (American
    Express). `site` is ORC's CE site number, usually CX_1. `company` is an
    optional display name for postings — tenant IDs are opaque slugs."""
    tenant: str
    region: str
    site: str
    company: str | None = None


class JsonLdBoard(BaseModel):
    """One SuccessFactors / iCIMS / TalentBrew board scraped via coarse HTML/RSS
    + schema.org JSON-LD detail. `base_url` is the branded careers domain (a
    per-tenant lookup — not derivable from the company name); `slug` is a short
    id for the connector name `{family}:{slug}`."""
    family: Literal["successfactors", "icims", "talentbrew"]
    slug: str
    base_url: str
    company: str | None = None


class EightfoldTenant(BaseModel):
    """One Eightfold.ai board. `domain` is the API's required domain= param
    (not always the corporate TLD — Northrop Grumman is ngc.com); `flavor` is
    the API generation (pcsx | apply_v2), defaulting to pcsx with a fetch-time
    fallback. `company` is an optional display name."""
    slug: str
    domain: str
    flavor: Literal["pcsx", "apply_v2"] = "pcsx"
    company: str | None = None


class PhenomBoard(BaseModel):
    """One Phenom People career site, addressed by its branded careers URL
    (careers.fisglobal.com — not machine-derivable from the company name, so
    hand-curated). `company` is an optional display name."""
    careers_url: str
    company: str | None = None


class TaleoBoard(BaseModel):
    """One modern Oracle Taleo career section, addressed by (tenant, section) —
    e.g. cinfin.taleo.net/careersection/ex is tenant=cinfin, section=ex. Only
    the modern template is supported (legacy jobsearch.ajax tenants and
    off-Taleo migrations fail the fingerprint verify)."""
    tenant: str
    section: str
    company: str | None = None


class AvatureBoard(BaseModel):
    """One Avature careers site, addressed by its (branded) SearchJobs URL —
    e.g. careers.jacobs.com/en_US/careers/SearchJobs. Avature serves an HTTP 202
    empty body to non-browser clients, so this polls on the headless tier.
    Hand-curated (branded domains aren't machine-fingerprintable)."""
    careers_url: str
    company: str


class SourcesConfig(BaseModel):
    greenhouse: list[str] = Field(default_factory=list)
    lever: list[str] = Field(default_factory=list)
    ashby: list[str] = Field(default_factory=list)
    workable: list[str] = Field(default_factory=list)
    smartrecruiters: list[str] = Field(default_factory=list)
    rippling: list[str] = Field(default_factory=list)
    personio: list[str] = Field(default_factory=list)
    recruitee: list[str] = Field(default_factory=list)
    teamtailor: list[str] = Field(default_factory=list)
    workday: list[WorkdayTenant] = Field(default_factory=list)
    oraclecloud: list[OracleCloudTenant] = Field(default_factory=list)
    eightfold: list[EightfoldTenant] = Field(default_factory=list)
    jsonld_boards: list[JsonLdBoard] = Field(default_factory=list)
    phenom: list[PhenomBoard] = Field(default_factory=list)
    taleo: list[TaleoBoard] = Field(default_factory=list)
    avature: list[AvatureBoard] = Field(default_factory=list)
    hn_who_is_hiring: AggregatorConfig = Field(default_factory=AggregatorConfig)
    remotive: AggregatorConfig = Field(default_factory=AggregatorConfig)
    remoteok: AggregatorConfig = Field(default_factory=AggregatorConfig)
    hiringcafe: HiringCafeConfig = Field(default_factory=HiringCafeConfig)
    adzuna: AdzunaConfig = Field(default_factory=AdzunaConfig)


# Source families whose entries are bare slug strings (vs. structured boards).
SLUG_SOURCE_FAMILIES: tuple[str, ...] = (
    "greenhouse", "lever", "ashby", "workable", "smartrecruiters",
    "rippling", "personio", "recruitee", "teamtailor",
)


class SchedulesConfig(BaseModel):
    ats_minutes: int = Field(default=10, ge=1)
    slow_minutes: int = Field(default=15, ge=1)
    discovery_hours: int = Field(default=24, ge=1)
    headless_minutes: int = Field(default=45, ge=1)
    digest_cron: str = "0 13 * * 1"  # APScheduler cron: Mondays 13:00 UTC

    @field_validator("digest_cron")
    @classmethod
    def _check_cron(cls, v: str) -> str:
        return _check_crontab(v)


class Secrets(BaseModel):
    ntfy_topic_url: str = ""       # empty -> no ntfy sink
    discord_webhook_url: str = ""  # empty -> no Discord sink, no gap digest
    anthropic_api_key: str = ""  # empty string allowed when relevance is disabled
    google_api_key: str = ""     # empty string allowed when not using provider=gemini
    ollama_api_key: str = ""     # empty string allowed when not using provider=ollama
    tailor_endpoint_url: str = ""    # tailor deep-link base URL (empty -> no deep-link)
    tailor_signing_secret: str = ""  # HMAC secret for the deep-link token
    ops_ntfy_topic_url: str = ""      # separate ntfy topic for ops alerts (empty -> ops alerts off)
    ops_discord_webhook_url: str = "" # separate Discord webhook for ops alerts
    heartbeat_url: str = ""           # healthchecks.io-style dead-man's-switch ping (empty -> no ping)
    gmail_address: str = ""           # Gmail ingestion (empty -> feature off)
    gmail_app_password: str = ""      # Gmail app password (requires 2-Step Verification)
    adzuna_app_id: str = ""       # Adzuna API app_id (empty -> adzuna connector off)
    adzuna_app_key: str = ""      # Adzuna API app_key


class GmailConfig(BaseModel):
    # Gmail ingestion (local scheduler only): hourly read-only IMAP sweep
    # writing suggest-only board badges. Feature is env-gated on the two
    # JOB_AGG_GMAIL_* secrets; these knobs only tune a configured instance.
    check_cron: str = "0 * * * *"
    first_run_days: int = Field(default=3, ge=1)
    lookback_max_days: int = Field(default=7, ge=1)
    max_messages_per_run: int = Field(default=200, ge=1)

    @field_validator("check_cron")
    @classmethod
    def _check_cron(cls, v: str) -> str:
        return _check_crontab(v)


class AppConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    filters: FiltersConfig = Field(default_factory=FiltersConfig)
    quiet_hours: QuietHoursConfig | None = None
    sources: SourcesConfig = Field(default_factory=SourcesConfig)
    schedules: SchedulesConfig = Field(default_factory=SchedulesConfig)
    secrets: Secrets = Field(default_factory=Secrets)
    discovery: DiscoveryConfig = Field(default_factory=DiscoveryConfig)
    relevance: RelevanceConfig = Field(default_factory=RelevanceConfig)
    gap_analysis: GapAnalysisConfig = Field(default_factory=GapAnalysisConfig)
    resume_draft: ResumeDraftConfig = Field(default_factory=ResumeDraftConfig)
    tailoring: TailoringConfig = Field(default_factory=TailoringConfig)
    board: BoardConfig = Field(default_factory=BoardConfig)
    audit: AuditConfig = Field(default_factory=AuditConfig)
    coach: CoachConfig = Field(default_factory=CoachConfig)
    ops_notify: OpsNotifyConfig = Field(default_factory=OpsNotifyConfig)
    http: HttpConfig = Field(default_factory=HttpConfig)
    gmail: GmailConfig = Field(default_factory=GmailConfig)
