"""Step order is DERIVED, never stored — readiness.check() is the only source
of truth for what is configured, so the wizard and the Overview page cannot
disagree."""
import pytest

from src.config import AppConfig, Secrets
from src.settings.documents import Documents
from src.web.wizard.steps import (
    WIZARD_STEPS, build_context, next_step, step_by_slug, step_states,
)

ALL_SLUGS = ["llm", "resume", "review", "companies", "notifications", "preview"]


def _ctx(cfg=None, *, documents=None, secrets=(), preview_done=False):
    source = lambda name: "stored" if name in secrets else "unset"  # noqa: E731
    return build_context(
        cfg or AppConfig(),
        documents or Documents(),
        source,
        preview_done=preview_done,
    )


def test_steps_are_in_the_documented_order():
    assert [s.slug for s in WIZARD_STEPS] == ALL_SLUGS


def test_fresh_install_starts_at_the_llm_step():
    assert next_step(_ctx(), skipped=set()).slug == "llm"


def test_llm_step_is_incomplete_while_scoring_is_off():
    """readiness raises llm_no_key only when relevance.enabled, so keying this
    step on that code ALONE would mark it complete on a fresh install and skip
    the step entirely."""
    ctx = _ctx(AppConfig(relevance={"enabled": False}))
    assert "llm_no_key" not in ctx.codes
    assert next_step(ctx, skipped=set()).slug == "llm"


def test_llm_step_completes_when_enabled_with_a_key():
    cfg = AppConfig(
        relevance={"enabled": True, "provider": "anthropic"},
        secrets=Secrets(anthropic_api_key="k"),
    )
    ctx = _ctx(cfg, secrets={"anthropic_api_key"})
    assert next_step(ctx, skipped=set()).slug == "resume"


def test_local_ollama_completes_the_llm_step_without_a_key():
    cfg = AppConfig(relevance={
        "enabled": True, "provider": "ollama", "ollama_host": "http://ollama:11434",
    })
    assert next_step(_ctx(cfg), skipped=set()).slug == "resume"


def test_skipping_advances_past_a_step():
    assert next_step(_ctx(), skipped={"llm"}).slug == "resume"


def test_resume_step_completes_with_a_resume_document():
    ctx = _ctx(documents=Documents(resume_text="experience"))
    assert next_step(ctx, skipped={"llm"}).slug == "review"


def test_review_needs_titles_max_age_and_a_profile():
    cfg = AppConfig(filters={"titles": ["platform engineer"], "max_age_days": 2})
    ctx = _ctx(cfg, documents=Documents(resume_text="r", profile="# Me"))
    assert next_step(ctx, skipped={"llm"}).slug == "companies"


def test_review_is_incomplete_without_max_age():
    """The max_age_days trap: None means the first run alerts on every posting
    on every board."""
    cfg = AppConfig(filters={"titles": ["x"]})
    ctx = _ctx(cfg, documents=Documents(resume_text="r", profile="# Me"))
    assert next_step(ctx, skipped={"llm"}).slug == "review"


def test_review_is_incomplete_without_a_profile():
    cfg = AppConfig(filters={"titles": ["x"], "max_age_days": 2})
    ctx = _ctx(cfg, documents=Documents(resume_text="r"))
    assert next_step(ctx, skipped={"llm"}).slug == "review"


def test_companies_completes_with_a_source():
    cfg = AppConfig(
        filters={"titles": ["x"], "max_age_days": 2},
        sources={"greenhouse": ["stripe"]},
    )
    ctx = _ctx(cfg, documents=Documents(resume_text="r", profile="# Me"))
    assert next_step(ctx, skipped={"llm"}).slug == "notifications"


def test_notifications_completes_with_a_sink():
    cfg = AppConfig(
        filters={"titles": ["x"], "max_age_days": 2},
        sources={"greenhouse": ["stripe"]},
        secrets=Secrets(ntfy_topic_url="https://ntfy.sh/t"),
    )
    ctx = _ctx(cfg, documents=Documents(resume_text="r", profile="# Me"),
               secrets={"ntfy_topic_url"})
    assert next_step(ctx, skipped={"llm"}).slug == "preview"


def test_everything_done_returns_none():
    cfg = AppConfig(
        filters={"titles": ["x"], "max_age_days": 2},
        sources={"greenhouse": ["stripe"]},
        secrets=Secrets(ntfy_topic_url="https://ntfy.sh/t"),
    )
    ctx = _ctx(cfg, documents=Documents(resume_text="r", profile="# Me"),
               secrets={"ntfy_topic_url"}, preview_done=True)
    assert next_step(ctx, skipped={"llm"}) is None


def test_everything_skipped_returns_none():
    assert next_step(_ctx(), skipped=set(ALL_SLUGS)) is None


def test_step_states_marks_done_skipped_and_current():
    states = step_states(_ctx(), skipped={"llm"})
    by_slug = {s.step.slug: s for s in states}
    assert by_slug["llm"].skipped is True
    assert by_slug["resume"].current is True
    assert by_slug["review"].current is False
    assert len(states) == len(WIZARD_STEPS)


def test_a_skipped_step_is_not_also_current():
    states = step_states(_ctx(), skipped={"llm"})
    assert not any(s.skipped and s.current for s in states)


def test_step_by_slug():
    assert step_by_slug("review").title
    assert step_by_slug("nope") is None


@pytest.mark.parametrize("slug", ALL_SLUGS)
def test_every_step_is_reachable_by_skipping_its_predecessors(slug):
    """No step can be stranded behind one that cannot be skipped."""
    index = ALL_SLUGS.index(slug)
    assert next_step(_ctx(), skipped=set(ALL_SLUGS[:index])).slug == slug


def test_companies_step_is_incomplete_on_a_fresh_install():
    """Three aggregators ship enabled, so readiness's nothing_polled never
    fires here — the wizard must still ask which boards to poll."""
    ctx = _ctx()  # plain AppConfig(), nothing disabled
    assert "nothing_polled" not in ctx.codes
    assert next_step(ctx, skipped={"llm", "resume", "review"}).slug == "companies"
