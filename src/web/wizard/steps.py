"""Which wizard step comes next, derived rather than stored.

The only persisted wizard state is the set of steps the user skipped and the
pending draft (src/state_sqlite.py: SqliteWizardStore). Everything else is a
function of the settings that are actually in effect, which is what keeps this
page and /settings/overview from ever disagreeing: both read
readiness.check()."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable

from src.config import AppConfig, SLUG_SOURCE_FAMILIES
from src.settings.documents import Documents
from src.web.settings.readiness import check, STRUCTURED_FAMILIES


@dataclass(frozen=True)
class StepContext:
    cfg: AppConfig
    documents: Documents
    codes: frozenset[str]
    preview_done: bool
    # Delivery channels whose secret is set, for the notifications summary.
    channels: tuple[str, ...] = ()


@dataclass(frozen=True)
class WizardStep:
    slug: str
    title: str
    blurb: str
    complete: Callable[[StepContext], bool]


@dataclass(frozen=True)
class StepState:
    step: WizardStep
    done: bool
    skipped: bool
    current: bool          # the step being viewed (not necessarily the next one)
    index: int = 0         # 1-based position, for the rail marker
    summary: str | None = None
    resume: bool = False   # next_step(): where Save/Skip will land
    link: bool = False     # rendered as a link in the rail


def _llm_done(ctx: StepContext) -> bool:
    """Scoring must be ON as well as keyed.

    readiness raises llm_no_key only when relevance.enabled is true, so
    `"llm_no_key" not in codes` is vacuously true on a fresh install — keying
    this step on the code alone would mark it complete before the user has
    done anything and skip the step entirely."""
    return ctx.cfg.relevance.enabled and "llm_no_key" not in ctx.codes


def _review_done(ctx: StepContext) -> bool:
    """Titles, a posting-age bound, and something to grade against.

    llm_no_profile is NOT used here even though it looks like the right code:
    it is conditional on relevance.enabled, so it disappears when the user
    skips the LLM step — but the profile document is still what this step
    produces. Check the document directly."""
    return (
        "no_titles" not in ctx.codes
        and "no_max_age" not in ctx.codes
        and bool(ctx.documents.profile)
    )


def _companies_done(ctx: StepContext) -> bool:
    """At least one company board the user chose, the starter pack, or discovery turned on.

    Deliberately NOT readiness's `nothing_polled`: three aggregator feeds
    (hn_who_is_hiring, remotive, remoteok) ship enabled, so that code never
    fires on a fresh install and this step would be skipped before the user
    was ever asked. Overview's question ("is anything polled at all?") and
    this one ("have you chosen where to look?") are different questions, and
    a default-on background feed is not a choice the user made."""
    sources = ctx.cfg.sources
    for family in (*SLUG_SOURCE_FAMILIES, *STRUCTURED_FAMILIES):
        if getattr(sources, family, None):
            return True
    return ctx.cfg.discovery.enabled or ctx.cfg.discovery.starter_pack


WIZARD_STEPS: tuple[WizardStep, ...] = (
    WizardStep(
        "llm", "Connect an LLM",
        "Scoring grades every posting against your profile. Optional — without "
        "it you get keyword matches only.",
        _llm_done,
    ),
    WizardStep(
        "resume", "Your résumé",
        "Upload it once. It becomes the résumé document, and the next step "
        "drafts your profile and filters from it.",
        lambda ctx: bool(ctx.documents.resume_text),
    ),
    WizardStep(
        "review", "Profile and filters",
        "What counts as a good match, and the hard gates every posting passes "
        "before it is scored.",
        _review_done,
    ),
    WizardStep(
        "companies", "Where to look",
        "The job boards to poll.",
        _companies_done,
    ),
    WizardStep(
        "notifications", "How to reach you",
        "Where matches are delivered.",
        lambda ctx: "no_sink" not in ctx.codes,
    ),
    WizardStep(
        "preview", "What would match",
        "A one-off look at real postings, with nothing delivered and nothing "
        "marked as seen.",
        lambda ctx: ctx.preview_done,
    ),
)

_BY_SLUG = {s.slug: s for s in WIZARD_STEPS}


def step_by_slug(slug: str) -> WizardStep | None:
    return _BY_SLUG.get(slug)


_CHANNELS = (("ntfy_topic_url", "ntfy"), ("discord_webhook_url", "Discord"))
_PROVIDERS = {"anthropic": "Anthropic", "gemini": "Gemini", "ollama": "Ollama"}


def _count(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _company_count(cfg: AppConfig) -> int:
    return sum(len(getattr(cfg.sources, f, None) or ())
               for f in (*SLUG_SOURCE_FAMILIES, *STRUCTURED_FAMILIES))


def _llm_summary(ctx: StepContext) -> str:
    r = ctx.cfg.relevance
    return f"{_PROVIDERS.get(r.provider, r.provider)} · {r.model}"


def _review_summary(ctx: StepContext) -> str:
    f = ctx.cfg.filters
    parts = [_count(len(f.titles), "title", "titles")]
    if f.max_age_days is not None:
        parts.append("last " + _count(f.max_age_days, "day", "days"))
    return " · ".join(parts)


def _companies_summary(ctx: StepContext) -> str:
    n = _company_count(ctx.cfg)
    if n == 0:
        return "Discovery on"
    text = _count(n, "company", "companies")
    return text + " · discovery on" if ctx.cfg.discovery.enabled else text


_DONE_SUMMARY: dict[str, Callable[[StepContext], str]] = {
    "llm": _llm_summary,
    "resume": lambda ctx: "Résumé saved",
    "review": _review_summary,
    "companies": _companies_summary,
    "notifications": lambda ctx: " · ".join(ctx.channels),
    "preview": lambda ctx: "Preview ran",
}
# Skipping these two changes what the app does, so say what.
_SKIPPED_SUMMARY = {
    "llm": "Skipped — keyword matches only",
    "notifications": "Skipped — no alerts",
}


def step_summary(step: WizardStep, ctx: StepContext, *, skipped: bool) -> str | None:
    """The rail's one-line note under a step title. Done wins over skipped: a
    step skipped here and finished later in Settings shows what it produced."""
    if step.complete(ctx):
        return _DONE_SUMMARY[step.slug](ctx)
    if skipped:
        return _SKIPPED_SUMMARY.get(step.slug, "Skipped")
    return None


def build_context(
    cfg: AppConfig,
    documents: Documents,
    secret_source: Callable[[str], str],
    *,
    preview_done: bool,
) -> StepContext:
    codes = frozenset(
        w.code for w in check(
            cfg, has_profile=bool(documents.profile), secret_source=secret_source,
        )
    )
    channels = tuple(label for name, label in _CHANNELS if secret_source(name) != "unset")
    return StepContext(
        cfg=cfg, documents=documents, codes=codes, preview_done=preview_done,
        channels=channels,
    )


def next_step(ctx: StepContext, skipped: Iterable[str]) -> WizardStep | None:
    """The first step that is neither complete nor skipped, or None when the
    wizard has nothing left to ask."""
    skipped = set(skipped)
    for step in WIZARD_STEPS:
        if step.slug not in skipped and not step.complete(ctx):
            return step
    return None


def step_states(
    ctx: StepContext, skipped: Iterable[str], *, viewed: str | None,
) -> list[StepState]:
    """Every step with its status, for the progress rail. `viewed` is the
    step page being rendered (None on /wizard/done). A step is a link when
    it is not the one on screen and is done, skipped, or where Save/Skip
    would land — so someone who went back can jump forward again."""
    skipped = set(skipped)
    resume = next_step(ctx, skipped)
    states = []
    for i, step in enumerate(WIZARD_STEPS, start=1):
        done = step.complete(ctx)
        is_skipped = step.slug in skipped
        is_resume = resume is not None and step.slug == resume.slug
        is_viewed = step.slug == viewed
        states.append(StepState(
            step=step, done=done, skipped=is_skipped, current=is_viewed,
            index=i, summary=step_summary(step, ctx, skipped=is_skipped),
            resume=is_resume,
            link=not is_viewed and (done or is_skipped or is_resume),
        ))
    return states
