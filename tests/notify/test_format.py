from datetime import datetime, timedelta, timezone

import pytest
from freezegun import freeze_time

from src.filters import Decision
from src.models import NormalizedPosting
from src.notify.format import format_payload


NOW = datetime(2026, 4, 30, 12, 0, 0, tzinfo=timezone.utc)


def _post(**kw) -> NormalizedPosting:
    base = dict(
        job_id="greenhouse:stripe:1",
        title="Senior Backend Engineer",
        company="Stripe",
        location_text="Remote, US",
        location_tags=frozenset({"remote"}),
        seniority="senior",
        stack=frozenset({"python", "go"}),
        comp_min=180_000,
        comp_max=240_000,
        apply_url="https://x",
        description="",
        posted_at=NOW - timedelta(minutes=2),
        source="greenhouse:stripe",
    )
    base.update(kw)
    return NormalizedPosting(**base)


@freeze_time(NOW)
def test_format_basic():
    pl = format_payload(_post(), Decision(allow=True), stack_filter=["python", "go", "react"])
    assert pl.title.startswith("Senior Backend Engineer @ Stripe")
    assert "Remote, US" in pl.location
    assert pl.comp == "$180k-$240k"
    assert set(pl.stack_matched) == {"python", "go"}
    assert pl.posted == "2 min ago"
    assert pl.seniority == "senior"


@freeze_time(NOW)
def test_format_unknown_location_tagged():
    p = _post(location_text="San Francisco, CA", location_tags=frozenset({"unknown_location"}))
    pl = format_payload(p, Decision(allow=True, unknowns=["location"]), stack_filter=["python"])
    assert pl.location.startswith("[loc?]")


@freeze_time(NOW)
def test_format_no_comp_omitted():
    pl = format_payload(_post(comp_min=None, comp_max=None), Decision(allow=True), stack_filter=["python"])
    assert pl.comp is None


@freeze_time(NOW)
def test_format_unknown_posted():
    pl = format_payload(_post(posted_at=None), Decision(allow=True), stack_filter=["python"])
    assert pl.posted == "unknown"


@freeze_time(NOW)
def test_format_tags_include_seniority_remote_and_stack():
    pl = format_payload(_post(), Decision(allow=True), stack_filter=["python", "go"])
    assert "senior" in pl.tags
    assert "remote" in pl.tags
    assert "python" in pl.tags
    assert "go" in pl.tags


def test_format_payload_with_high_score_sets_max_priority():
    """A score >= score_high sets relevance_priority to max."""
    from src.relevance import Score

    score = Score(value=9, rationale="Strong fit", is_fallback=False)

    pl = format_payload(_post(), Decision(allow=True), stack_filter=["python", "go"], score=score, score_high=7, score_low=3)

    assert pl.relevance_score == 9
    assert pl.relevance_rationale == "Strong fit"
    assert pl.relevance_priority == "max"


def test_format_payload_with_low_score_sets_min_priority():
    from src.relevance import Score

    score = Score(value=2, rationale="Wrong stack", is_fallback=False)

    pl = format_payload(_post(), Decision(allow=True), stack_filter=["python"], score=score, score_high=7, score_low=3)

    assert pl.relevance_priority == "min"


def test_format_payload_with_default_score_sets_default_priority():
    from src.relevance import Score

    score = Score(value=5, rationale="Borderline", is_fallback=False)

    pl = format_payload(_post(), Decision(allow=True), stack_filter=["python"], score=score, score_high=7, score_low=3)

    assert pl.relevance_priority == "default"


def test_format_payload_with_fallback_score_sets_default_priority():
    """Fail-open: LLM unavailable → default priority, score=None."""
    from src.relevance import Score

    score = Score(value=None, rationale="(LLM unavailable)", is_fallback=True)

    pl = format_payload(_post(), Decision(allow=True), stack_filter=["python"], score=score, score_high=7, score_low=3)

    assert pl.relevance_score is None
    assert pl.relevance_rationale == "(LLM unavailable)"
    assert pl.relevance_priority == "default"


def test_format_payload_without_score_uses_defaults():
    """When relevance is disabled, score=None → all relevance_* fields default."""
    pl = format_payload(_post(), Decision(allow=True), stack_filter=["python"], score=None, score_high=7, score_low=3)

    assert pl.relevance_score is None
    assert pl.relevance_rationale is None
    assert pl.relevance_priority == "default"


@freeze_time(NOW)
def test_format_payload_includes_gaps():
    pl = format_payload(_post(), Decision(allow=True), stack_filter=["python"], gaps=["Kubernetes", "Kafka"])
    assert pl.gaps == ["Kubernetes", "Kafka"]


@freeze_time(NOW)
def test_format_payload_gaps_default_empty():
    pl = format_payload(_post(), Decision(allow=True), stack_filter=["python"])
    assert pl.gaps == []


def test_format_payload_sets_tailor_url():
    from src.notify.format import format_payload
    p = _post()
    d = Decision(allow=True)
    payload = format_payload(p, d, stack_filter=[], tailor_url="https://ep.example?job_id=j&t=tok")
    assert payload.tailor_url == "https://ep.example?job_id=j&t=tok"
    assert format_payload(p, d, stack_filter=[]).tailor_url is None   # defaults to None
