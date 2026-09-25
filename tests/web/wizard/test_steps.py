"""Step order is DERIVED, never stored — readiness.check() is the only source
of truth for what is configured, so the wizard and the Overview page cannot
disagree."""
import pytest

from src.config import AppConfig, Secrets
from src.settings.documents import Documents
from src.web.wizard.steps import (
    WIZARD_STEPS, _companies_done, build_context, next_step, step_by_slug, step_states,
    step_summary,
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


def test_llm_step_is_incomplete_when_enabled_without_a_key():
    """Scoring on but unkeyed: readiness raises llm_no_key, and the step must
    stay incomplete. Without this, _llm_done could drop its code check and no
    test would notice."""
    cfg = AppConfig(relevance={"enabled": True, "provider": "anthropic"})
    ctx = _ctx(cfg)  # no secrets, so anthropic_api_key is unset
    assert "llm_no_key" in ctx.codes
    assert next_step(ctx, skipped=set()).slug == "llm"


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
    states = step_states(_ctx(), skipped={"llm"}, viewed="resume")
    by_slug = {s.step.slug: s for s in states}
    assert by_slug["llm"].skipped is True
    assert by_slug["resume"].current is True
    assert by_slug["review"].current is False
    assert len(states) == len(WIZARD_STEPS)


def test_the_resume_step_is_never_a_skipped_one():
    states = step_states(_ctx(), skipped={"llm"}, viewed="llm")
    assert not any(s.skipped and s.resume for s in states)


def test_step_states_done_flag_is_accurate():
    """StepState.done must reflect actual completion. Without this, done could
    be hardcoded and no test would notice; Task 6's progress rail renders it."""
    cfg = AppConfig(
        filters={"titles": ["x"], "max_age_days": 2},
        sources={"greenhouse": ["stripe"]},
        secrets=Secrets(ntfy_topic_url="https://ntfy.sh/t"),
    )
    ctx = _ctx(cfg, documents=Documents(resume_text="r", profile="# Me"),
               secrets={"ntfy_topic_url"})
    states = step_states(ctx, skipped=set(), viewed=None)
    by_slug = {s.step.slug: s for s in states}
    # At this point: llm (incomplete), resume (complete), review (complete),
    # companies (complete), notifications (complete), preview (incomplete)
    assert by_slug["llm"].done is False
    assert by_slug["resume"].done is True
    assert by_slug["review"].done is True
    assert by_slug["companies"].done is True
    assert by_slug["notifications"].done is True
    assert by_slug["preview"].done is False


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


def _pack(monkeypatch, n):
    from src.starter_pack import PackSlug, StarterPack
    pack = StarterPack("t", tuple(PackSlug("lever", f"c{i}", None, "us", 1) for i in range(n)), ())
    monkeypatch.setattr("src.starter_pack.default_pack", lambda: pack)


def test_companies_done_with_starter_pack_only(monkeypatch):
    _pack(monkeypatch, 3)
    cfg = AppConfig.model_validate({"discovery": {"starter_pack": True}})
    assert _companies_done(_ctx(cfg))


def test_companies_not_done_by_an_empty_starter_pack(monkeypatch):
    _pack(monkeypatch, 0)
    cfg = AppConfig.model_validate({"discovery": {"starter_pack": True}})
    assert not _companies_done(_ctx(cfg))


# ---- summaries ----

def test_llm_summary_names_provider_and_model():
    cfg = AppConfig(relevance={"enabled": True, "provider": "anthropic", "model": "claude-haiku"},
                    secrets=Secrets(anthropic_api_key="k"))
    ctx = _ctx(cfg, secrets={"anthropic_api_key"})
    assert step_summary(step_by_slug("llm"), ctx, skipped=False) == "Anthropic · claude-haiku"


def test_llm_skipped_summary_names_the_consequence():
    assert step_summary(step_by_slug("llm"), _ctx(), skipped=True) == "Skipped — keyword matches only"


def test_notifications_skipped_summary_names_the_consequence():
    assert step_summary(step_by_slug("notifications"), _ctx(), skipped=True) == "Skipped — no alerts"


@pytest.mark.parametrize("slug", ["resume", "review", "companies", "preview"])
def test_other_skipped_steps_just_say_skipped(slug):
    assert step_summary(step_by_slug(slug), _ctx(), skipped=True) == "Skipped"


def test_an_unreached_step_has_no_summary():
    assert step_summary(step_by_slug("resume"), _ctx(), skipped=False) is None


def test_done_wins_over_skipped():
    """A step skipped in the wizard and later completed from Settings shows
    what it produced, not "Skipped"."""
    ctx = _ctx(documents=Documents(resume_text="r"))
    assert step_summary(step_by_slug("resume"), ctx, skipped=True) == "Résumé saved"


def test_review_summary_counts_titles_and_age():
    cfg = AppConfig(filters={"titles": ["a", "b"], "max_age_days": 7})
    ctx = _ctx(cfg, documents=Documents(profile="# Me"))
    assert step_summary(step_by_slug("review"), ctx, skipped=False) == "2 titles · last 7 days"


def test_review_summary_singulars():
    cfg = AppConfig(filters={"titles": ["a"], "max_age_days": 1})
    ctx = _ctx(cfg, documents=Documents(profile="# Me"))
    assert step_summary(step_by_slug("review"), ctx, skipped=False) == "1 title · last 1 day"


def test_companies_summary_counts_boards_and_notes_discovery():
    cfg = AppConfig(sources={"greenhouse": ["stripe", "figma"], "lever": ["x"]},
                    discovery={"enabled": True})
    assert step_summary(step_by_slug("companies"), _ctx(cfg), skipped=False) == "3 companies · discovery on"


def test_companies_summary_counts_structured_boards():
    cfg = AppConfig(sources={"workday": [{"tenant": "acme", "region": "wd1", "site": "External"}]})
    assert step_summary(step_by_slug("companies"), _ctx(cfg), skipped=False) == "1 company"


def test_companies_summary_discovery_only():
    cfg = AppConfig(discovery={"enabled": True})
    assert step_summary(step_by_slug("companies"), _ctx(cfg), skipped=False) == "Discovery on"


def test_notifications_summary_lists_channels_in_order():
    ctx = _ctx(secrets={"ntfy_topic_url", "discord_webhook_url"})
    assert step_summary(step_by_slug("notifications"), ctx, skipped=False) == "ntfy · Discord"


def test_resume_and_preview_done_summaries():
    ctx = _ctx(documents=Documents(resume_text="r"), preview_done=True)
    assert step_summary(step_by_slug("resume"), ctx, skipped=False) == "Résumé saved"
    assert step_summary(step_by_slug("preview"), ctx, skipped=False) == "Preview ran"


# ---- viewed / resume / link ----

def test_states_carry_index_and_summary():
    states = step_states(_ctx(), skipped={"llm"}, viewed="resume")
    assert [s.index for s in states] == [1, 2, 3, 4, 5, 6]
    assert states[0].summary == "Skipped — keyword matches only"


def test_viewed_step_is_current_and_never_a_link():
    by = {s.step.slug: s for s in step_states(_ctx(), skipped={"llm"}, viewed="llm")}
    assert by["llm"].current is True and by["llm"].link is False
    assert sum(s.current for s in by.values()) == 1


def test_resume_step_is_linked_when_viewing_an_earlier_one():
    by = {s.step.slug: s for s in step_states(_ctx(), skipped={"llm"}, viewed="llm")}
    assert by["resume"].resume is True
    assert by["resume"].link is True


def test_done_and_skipped_steps_are_links_later_steps_are_not():
    ctx = _ctx(documents=Documents(resume_text="r"))   # resume done
    by = {s.step.slug: s for s in step_states(ctx, skipped={"llm"}, viewed="review")}
    assert by["llm"].link is True        # skipped
    assert by["resume"].link is True     # done
    assert by["review"].link is False    # viewed (and the resume step)
    assert by["companies"].link is False  # not reached
    assert by["preview"].link is False


def test_no_viewed_step_on_the_done_page():
    states = step_states(_ctx(), skipped=set(ALL_SLUGS), viewed=None)
    assert not any(s.current for s in states)
    assert not any(s.resume for s in states)   # next_step() is None
