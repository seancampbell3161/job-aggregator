"""Which page owns which settings.

A hand-built section claims the paths it lays out by hand; Advanced renders
whatever each top-level config key has left over. tests/web/settings/
test_sections.py enforces the partition, so a flag added to src/config.py can
never end up on no page at all."""
from __future__ import annotations

from dataclasses import dataclass, field as dc_field

from src.settings.boards import BOARD_FAMILIES
from src.settings.fields import FieldSpec, editable_fields, field_map


@dataclass(frozen=True)
class Section:
    slug: str
    title: str
    template: str
    paths: tuple[str, ...] = ()
    secrets: tuple[str, ...] = ()
    blurb: str = ""
    bulk_save: bool = True  # False: this section has no whole-page form; every write goes through its own routes


SECTIONS: tuple[Section, ...] = (
    Section(
        slug="overview", title="Overview", template="settings_overview.html",
        blurb="What this instance will and will not do right now.",
    ),
    Section(
        slug="filters", title="Filters", template="settings_filters.html",
        blurb="Hard gates every posting passes before it is scored.",
        paths=(
            "filters.titles",
            "filters.seniority_allow",
            "filters.stack_any_of",
            "filters.comp_floor_usd",
            "filters.max_age_days",
            "filters.blocked_employment_types",
            "filters.blocked_companies",
            "filters.location.allowed_countries",
            "filters.location.allowed_cities",
            "filters.location.remote_policy",
            "filters.location.allow_unknown",
        ),
    ),
    Section(
        slug="profile", title="Profile", template="settings_profile.html",
        blurb="What the scorer grades each posting against.",
    ),
    Section(
        slug="llm", title="LLM", template="settings_llm.html",
        blurb="Scoring, gap analysis, tailoring, and coaching.",
        paths=(
            "relevance.enabled", "relevance.provider", "relevance.model",
            "relevance.score_high", "relevance.score_low",
            "relevance.timeout_seconds", "relevance.ollama_host",
            "gap_analysis.enabled", "gap_analysis.provider", "gap_analysis.model",
            "resume_draft.provider", "resume_draft.model", "resume_draft.timeout_seconds",
            "tailoring.enabled", "tailoring.provider", "tailoring.model",
            "coach.enabled", "coach.provider", "coach.model",
        ),
        secrets=("anthropic_api_key", "google_api_key", "ollama_api_key"),
    ),
    Section(
        slug="notifications", title="Notifications", template="settings_notifications.html",
        blurb="Where matches and ops alerts go.",
        # ops_notify has no `enabled` flag — ops alerts turn on by setting one of
        # the ops URLs below. Its thresholds stay in Advanced.
        paths=("quiet_hours.timezone", "quiet_hours.start", "quiet_hours.end"),
        secrets=(
            "ntfy_topic_url", "discord_webhook_url",
            "ops_ntfy_topic_url", "ops_discord_webhook_url", "heartbeat_url",
        ),
    ),
    Section(
        slug="integrations", title="Integrations", template="settings_integrations.html",
        blurb=(
            "Credentials for optional data sources (Adzuna, Gmail ingestion) "
            "and the tailoring deep-link endpoint. None of these turn "
            "anything on by themselves — each feature also has its own "
            "enable flag elsewhere."
        ),
        secrets=(
            "adzuna_app_id", "adzuna_app_key", "gmail_address",
            "gmail_app_password", "tailor_endpoint_url",
        ),
    ),
    Section(
        slug="schedules", title="Schedules", template="settings_schedules.html",
        blurb="How often each tier runs. Cron fields are UTC, five-field crontab.",
        paths=(
            "schedules.ats_minutes", "schedules.slow_minutes",
            "schedules.discovery_hours", "schedules.headless_minutes",
            "schedules.digest_cron",
            "board.closed_check_cron", "board.digest_cron",
            "gmail.check_cron",
        ),
    ),
    Section(
        slug="companies", title="Companies", template="settings_companies.html",
        blurb=(
            "Every company board this instance polls directly. Add one by "
            "pasting its careers page; discovery finds more on its own."
        ),
        paths=tuple(f"sources.{family}" for family in BOARD_FAMILIES),
        bulk_save=False,  # Slug families (9) are KIND_CHIPS, not KIND_ROWS; structured families (7) are KIND_ROWS. Only KIND_ROWS are safe from form-based saves. Every write goes through row-specific routes, not generic bulk save.
    ),
    Section(
        slug="documents", title="Documents", template="settings_documents.html",
        blurb="Résumé and apply-kit data the tailoring features read.",
    ),
    Section(
        slug="advanced", title="Advanced", template="settings_advanced.html",
        blurb="Every remaining flag, generated from the settings model.",
    ),
    Section(
        slug="history", title="History", template="settings_history.html",
        blurb="Every saved version of these settings, and how to go back.",
    ),
    Section(
        slug="backup", title="Backup", template="settings_backup.html",
        blurb="Download everything as a file, or restore from one.",
    ),
)

CLAIMED_PATHS: frozenset[str] = frozenset(p for s in SECTIONS for p in s.paths)

# Secrets no settings page offers. tailor_signing_secret is generated on first
# boot by ConfigService.ensure_signing_secret() and editing it would silently
# invalidate every deep link already sent to the user's phone, so it stays
# CLI-only by design. The rest are claimed by the integrations section.
UNCLAIMED_SECRETS: frozenset[str] = frozenset({"tailor_signing_secret"})

# An Advanced group is titled after its config key, which stops being
# descriptive when a hand-built section claims most of the key. `sources` keeps
# only the aggregator feeds once Companies claims the sixteen board families.
GROUP_TITLES: dict[str, str] = {"sources": "Aggregators"}

_BY_SLUG = {s.slug: s for s in SECTIONS}


def section_by_slug(slug: str) -> Section | None:
    return _BY_SLUG.get(slug)


def section_fields(section: Section) -> tuple[FieldSpec, ...]:
    """The section's fields, in the order it claimed them."""
    fields = field_map()
    return tuple(fields[p] for p in section.paths)


@dataclass(frozen=True)
class AdvancedGroup:
    key: str
    title: str
    fields: tuple[FieldSpec, ...] = dc_field(default=())


def _groups() -> tuple[AdvancedGroup, ...]:
    buckets: dict[str, list[FieldSpec]] = {}
    for spec in editable_fields():
        if spec.path in CLAIMED_PATHS:
            continue
        buckets.setdefault(spec.root, []).append(spec)
    return tuple(
        AdvancedGroup(
            key=key,
            title=GROUP_TITLES.get(key, key.replace("_", " ").capitalize()),
            fields=tuple(specs),
        )
        for key, specs in buckets.items()
    )


_ADVANCED = _groups()
_ADVANCED_BY_KEY = {g.key: g for g in _ADVANCED}


def advanced_groups() -> tuple[AdvancedGroup, ...]:
    return _ADVANCED


def advanced_group(key: str) -> AdvancedGroup | None:
    return _ADVANCED_BY_KEY.get(key)


def group_section(group: AdvancedGroup) -> Section:
    """An AdvancedGroup dressed as a Section, so the shared save handler works
    on generated pages with no special cases."""
    return Section(
        slug=f"advanced/{group.key}",
        title=group.title,
        template="settings_advanced_group.html",
        paths=tuple(f.path for f in group.fields),
    )
