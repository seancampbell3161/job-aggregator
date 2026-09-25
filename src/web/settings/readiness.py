"""What this instance will and will not do, given its current settings.

Every check here is a configuration that validates cleanly and silently does
nothing useful — the class of problem the CLI-only era had no way to surface."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from src.config import AppConfig, SLUG_SOURCE_FAMILIES
# The single source of truth, shared with every LLM factory.
from src.llm.providers import PROVIDER_KEYS as _PROVIDER_KEYS

STRUCTURED_FAMILIES = (
    "workday", "oraclecloud", "eightfold", "jsonld_boards", "phenom", "taleo", "avature",
)
# Feeds that poll without any company being added. Public: the wizard's
# companies step tells the user which of these are already running, so
# "I added nothing" does not read as "nothing is polled".
AGGREGATOR_FAMILIES = ("hn_who_is_hiring", "remotive", "remoteok", "hiringcafe", "adzuna")


@dataclass(frozen=True)
class Warning:
    code: str
    message: str
    fix_slug: str


def _has_sources(cfg: AppConfig) -> bool:
    for family in (*SLUG_SOURCE_FAMILIES, *STRUCTURED_FAMILIES):
        if getattr(cfg.sources, family, None):
            return True
    return any(getattr(cfg.sources, name).enabled for name in AGGREGATOR_FAMILIES)


def check(
    cfg: AppConfig, *, has_profile: bool, secret_source: Callable[[str], str]
) -> list[Warning]:
    """Ordered worst-first; each warning links to the page that fixes it."""
    out: list[Warning] = []
    is_set = lambda name: secret_source(name) != "unset"  # noqa: E731

    if not cfg.filters.titles:
        out.append(Warning(
            "no_titles",
            "No job titles are set, so no posting can ever match. "
            "Add at least one title.",
            "filters",
        ))
    if not _has_sources(cfg) and not cfg.discovery.enabled and not cfg.discovery.starter_pack:
        out.append(Warning(
            "nothing_polled",
            "No company boards are configured and discovery is off, "
            "so nothing is being polled.",
            "advanced",
        ))
    if not is_set("ntfy_topic_url") and not is_set("discord_webhook_url"):
        out.append(Warning(
            "no_sink",
            "Neither ntfy nor Discord is configured — matches are scored "
            "but never delivered anywhere.",
            "notifications",
        ))
    if cfg.relevance.enabled:
        # .get(), not [] — a provider added to the Literal in src/config.py
        # without a matching entry here must not 500 this page (it's where
        # POST /setup/start sends a first-time user); skip the key check
        # rather than guess which secret an unknown provider would need.
        key = _PROVIDER_KEYS.get(cfg.relevance.provider)
        needs_key = not (cfg.relevance.provider == "ollama" and cfg.relevance.ollama_is_local)
        if key is not None and needs_key and not is_set(key):
            out.append(Warning(
                "llm_no_key",
                f"Scoring is on with provider {cfg.relevance.provider}, but no "
                f"API key is set — every posting will be delivered unscored.",
                "llm",
            ))
        if not has_profile:
            out.append(Warning(
                "llm_no_profile",
                "Scoring is on but there is no profile document to grade "
                "postings against, so scoring stays off.",
                "profile",
            ))
    if cfg.filters.max_age_days is None:
        out.append(Warning(
            "no_max_age",
            "No maximum posting age is set. The first run will alert on every "
            "posting currently on every board, not just new ones.",
            "filters",
        ))
    return out
