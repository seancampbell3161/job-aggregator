"""Plain-language label, hint and blank-value text for the settings a newcomer
edits by hand (the Filters, LLM, Notifications and Schedules sections, and
every secret a settings page offers).

The Advanced pages keep labels generated from key names and help parsed from
docs/CONFIG.md; that reference text still appears behind each curated field's
"more" disclosure. tests/settings/test_copy.py requires an entry for every
hand-built field and secret, and keeps config jargon out of this text."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Copy:
    label: str
    hint: str
    blank: str | None = None  # what an empty value means: select option text / placeholder


_SAME_PROVIDER = "Same as match scoring"
_SAME_MODEL = "Same as match scoring"

COPY: dict[str, Copy] = {
    # --- Filters -------------------------------------------------------------
    "filters.titles": Copy(
        "Job titles",
        "A posting must match one of these titles. Each one matches as a whole "
        "phrase, ignoring case, so add variants like “full-stack engineer” and "
        "“fullstack engineer” separately.",
    ),
    "filters.seniority_allow": Copy(
        "Seniority levels",
        "Keep postings whose title suggests one of these levels.",
    ),
    "filters.stack_any_of": Copy(
        "Technologies",
        "When a posting lists its technologies, it must include at least one of these.",
    ),
    "filters.comp_floor_usd": Copy(
        "Minimum salary (USD)",
        "Skip postings whose advertised minimum pay is below this. Postings that "
        "don't list pay are kept. 0 turns this off.",
    ),
    "filters.max_age_days": Copy(
        "Max posting age (days)",
        "Skip postings older than this. 2 keeps alerts fresh once you are set up.",
        blank="Any age",
    ),
    "filters.blocked_employment_types": Copy(
        "Skip these job types",
        "Postings of these types are skipped when the source says what type they "
        "are. Postings with an unknown type are kept.",
    ),
    "filters.blocked_companies": Copy(
        "Blocked companies",
        "Postings from these companies are always skipped. Whole words match, so "
        "“microsoft” covers Microsoft Corporation but “apple” doesn't match Applebee's.",
    ),
    "filters.location.allowed_countries": Copy(
        "Countries",
        "Two-letter country codes, like US or GB. Remote postings must be open to "
        "one of these. Needs at least one.",
    ),
    "filters.location.allowed_cities": Copy(
        "Cities",
        "On-site and hybrid postings must be in one of these cities.",
    ),
    "filters.location.remote_policy": Copy(
        "Remote jobs",
        "Whether a remote posting must be open to your countries, or any remote posting counts.",
    ),
    "filters.location.allow_unknown": Copy(
        "Keep postings with an unclear location",
        "When a location can't be read, send the posting on to scoring instead of skipping it.",
    ),
    # --- LLM: match scoring ----------------------------------------------------
    "relevance.enabled": Copy(
        "Score matches with AI",
        "Each posting that passes your filters gets a 0–10 fit score. When this is "
        "off, every posting that passes is sent to you unscored.",
    ),
    "relevance.provider": Copy(
        "Provider",
        "Which AI service scores postings. Ollama covers both a local server and Ollama Cloud.",
    ),
    "relevance.model": Copy(
        "Model",
        "The model name your provider expects, for example claude-haiku-4-5 or llama3.1:8b.",
    ),
    "relevance.score_high": Copy(
        "Phone alert at score",
        "Postings scoring this or higher get an instant phone push.",
    ),
    "relevance.score_low": Copy(
        "Hide at score",
        "Postings scoring this or lower are hidden from Matches. You can still find "
        "them under Rejected postings.",
    ),
    "relevance.timeout_seconds": Copy(
        "Time limit per posting (seconds)",
        "How long to wait for a score before giving up on that posting. Large local "
        "models need 20 or more.",
    ),
    "relevance.ollama_host": Copy(
        "Ollama address",
        "Where Ollama runs. Use https://ollama.com for Ollama Cloud (it needs the "
        "Ollama Cloud API key); any other address is treated as a local server.",
    ),
    # --- LLM: skill gaps -------------------------------------------------------
    "gap_analysis.enabled": Copy(
        "Find skill gaps",
        "Lists skills a posting asks for that your profile lacks, and sends a weekly summary.",
    ),
    "gap_analysis.provider": Copy("Provider", "Which AI service finds skill gaps.", blank=_SAME_PROVIDER),
    "gap_analysis.model": Copy("Model", "The model name your provider expects.", blank=_SAME_MODEL),
    # --- LLM: résumé drafts ----------------------------------------------------
    "resume_draft.provider": Copy(
        "Provider", "Which AI service reads your résumé and drafts from it.", blank=_SAME_PROVIDER,
    ),
    "resume_draft.model": Copy("Model", "The model name your provider expects.", blank=_SAME_MODEL),
    "resume_draft.timeout_seconds": Copy(
        "Time limit (seconds)",
        "How long to wait when reading your résumé or drafting from it.",
    ),
    # --- LLM: tailored résumés -------------------------------------------------
    "tailoring.enabled": Copy(
        "Tailor résumés",
        "Lets alerts link to a résumé tailored to that posting.",
    ),
    "tailoring.provider": Copy("Provider", "Which AI service tailors résumés.", blank=_SAME_PROVIDER),
    "tailoring.model": Copy("Model", "The model name your provider expects.", blank=_SAME_MODEL),
    # --- LLM: coach ------------------------------------------------------------
    "coach.enabled": Copy(
        "Show the coach",
        "Adds the Coach page, which suggests ways to raise your response rate.",
    ),
    "coach.provider": Copy("Provider", "Which AI service the coach uses.", blank=_SAME_PROVIDER),
    "coach.model": Copy("Model", "The model name your provider expects.", blank=_SAME_MODEL),
    # --- Notifications ---------------------------------------------------------
    "quiet_hours.timezone": Copy("Time zone", "Your time zone, for example Europe/London."),
    "quiet_hours.start": Copy("Starts at", "Local time when phone pushes pause."),
    "quiet_hours.end": Copy("Ends at", "Local time when phone pushes resume."),
    # --- Schedules -------------------------------------------------------------
    "schedules.ats_minutes": Copy(
        "Job boards — check every (minutes)",
        "Company job boards such as Greenhouse, Lever and Ashby.",
    ),
    "schedules.slow_minutes": Copy(
        "Aggregators — check every (minutes)",
        "HN Who's Hiring, Remotive, RemoteOK and Adzuna.",
    ),
    "schedules.discovery_hours": Copy(
        "Finding new companies — every (hours)",
        "How often to look for new company job boards to add.",
    ),
    "schedules.headless_minutes": Copy(
        "Browser-only boards — check every (minutes)",
        "Boards that need a real browser to read. These are slow, so keep this long.",
    ),
    "schedules.digest_cron": Copy(
        "Weekly skills summary — schedule",
        "When the skill-gap summary is sent. Default: Mondays at 13:00 UTC.",
    ),
    "board.closed_check_cron": Copy(
        "Closed-posting check — schedule",
        "When to mark applications whose posting has closed. Default: daily at 04:30 UTC.",
    ),
    "board.digest_cron": Copy(
        "Applications digest — schedule",
        "When the daily push about stale or closed applications goes out. Default: daily at 15:00 UTC.",
    ),
    "gmail.check_cron": Copy(
        "Gmail check — schedule",
        "When to read new application emails. Default: every hour, on the hour.",
    ),
    # --- Secrets ---------------------------------------------------------------
    "secrets.anthropic_api_key": Copy("Anthropic API key", "Needed when a feature uses Anthropic."),
    "secrets.google_api_key": Copy("Google API key", "Needed when a feature uses Gemini."),
    "secrets.ollama_api_key": Copy(
        "Ollama Cloud API key", "Only for Ollama Cloud. Leave empty for a local Ollama.",
    ),
    "secrets.ntfy_topic_url": Copy(
        "Phone push (ntfy topic URL)",
        "Your ntfy topic, for example https://ntfy.sh/your-topic. Leave empty for no phone pushes.",
    ),
    "secrets.discord_webhook_url": Copy(
        "Discord webhook URL",
        "Posts every match and the weekly skills summary to a Discord channel.",
    ),
    "secrets.ops_ntfy_topic_url": Copy(
        "Problem alerts — ntfy topic URL",
        "A separate topic for alerts when the app itself has trouble. Leave both "
        "problem-alert fields empty to turn these alerts off.",
    ),
    "secrets.ops_discord_webhook_url": Copy(
        "Problem alerts — Discord webhook URL",
        "A separate Discord channel for alerts when the app itself has trouble.",
    ),
    "secrets.heartbeat_url": Copy(
        "Heartbeat URL",
        "Pinged after every check, so a service like healthchecks.io can warn you if checks stop.",
    ),
    "secrets.adzuna_app_id": Copy(
        "Adzuna app ID", "A free key from developer.adzuna.com. Leave empty to skip Adzuna.",
    ),
    "secrets.adzuna_app_key": Copy("Adzuna app key", "The key that goes with the app ID."),
    "secrets.gmail_address": Copy(
        "Gmail address",
        "Lets the app read replies to your applications and suggest board moves. "
        "Leave empty to turn this off.",
    ),
    "secrets.gmail_app_password": Copy(
        "Gmail app password",
        "An app password, not your normal one. Google needs 2-Step Verification "
        "turned on before it will make one.",
    ),
    "secrets.tailor_endpoint_url": Copy(
        "Tailoring link address",
        "This app's address as your phone reaches it, used for the tailored-résumé "
        "links in alerts. Leave empty for no links.",
    ),
}


def field_copy(path: str) -> Copy | None:
    return COPY.get(path)


def secret_label(name: str) -> str:
    c = COPY.get(f"secrets.{name}")
    return c.label if c else name.replace("_", " ")
