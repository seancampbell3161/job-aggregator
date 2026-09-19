"""Which wizard step comes next, derived rather than stored.

The only persisted wizard state is the set of steps the user skipped and the
pending draft (src/state_sqlite.py: SqliteWizardStore). Everything else is a
function of the settings that are actually in effect, which is what keeps this
page and /settings/overview from ever disagreeing: both read
readiness.check()."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable

from src.config import AppConfig
from src.settings.documents import Documents
from src.web.settings.readiness import check


@dataclass(frozen=True)
class StepContext:
    cfg: AppConfig
    documents: Documents
    codes: frozenset[str]
    preview_done: bool


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
    current: bool


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
        lambda ctx: "nothing_polled" not in ctx.codes,
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
    return StepContext(
        cfg=cfg, documents=documents, codes=codes, preview_done=preview_done,
    )


def next_step(ctx: StepContext, skipped: Iterable[str]) -> WizardStep | None:
    """The first step that is neither complete nor skipped, or None when the
    wizard has nothing left to ask."""
    skipped = set(skipped)
    for step in WIZARD_STEPS:
        if step.slug not in skipped and not step.complete(ctx):
            return step
    return None


def step_states(ctx: StepContext, skipped: Iterable[str]) -> list[StepState]:
    """Every step with its status, for the progress rail."""
    skipped = set(skipped)
    current = next_step(ctx, skipped)
    return [
        StepState(
            step=step,
            done=step.complete(ctx),
            skipped=step.slug in skipped,
            current=current is not None and step.slug == current.slug,
        )
        for step in WIZARD_STEPS
    ]
