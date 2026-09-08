# tests/test_coach.py
"""Coach: config, snapshot builder, output parsing, provider trio, factory."""
from datetime import datetime, timezone

from src.config import AppConfig
from src.coach import CoachSnapshot, build_snapshot
from src.tailor.models import Bullet, Experience, Project, ResumeContent, Skill


def _raw_config(**overrides) -> dict:
    """Minimal valid AppConfig raw dict (secrets inline so no env is needed)."""
    raw = {
        "filters": {
            "titles": ["engineer"], "seniority_allow": ["senior"],
            "location": {}, "comp_floor_usd": 0, "stack_any_of": [],
        },
        "quiet_hours": {"timezone": "UTC", "start": "22:00", "end": "07:00"},
        "sources": {},
        "schedules": {"ats_minutes": 30, "slow_minutes": 360},
        "secrets": {"ntfy_topic_url": "https://ntfy.sh/x", "discord_webhook_url": ""},
        "relevance": {"enabled": True, "provider": "ollama", "model": "gpt-oss:120b"},
    }
    raw.update(overrides)
    return raw


def test_coach_config_defaults():
    cfg = AppConfig.model_validate(_raw_config())
    assert cfg.coach.enabled is True
    assert cfg.coach.provider is None
    assert cfg.coach.model is None
    assert cfg.coach.timeout_seconds == 120
    assert cfg.coach.max_jobs == 100
    assert cfg.coach.window_days == 90


def test_coach_config_overrides():
    cfg = AppConfig.model_validate(_raw_config(
        coach={"enabled": False, "provider": "anthropic", "model": "claude-haiku-4-5",
               "timeout_seconds": 60, "max_jobs": 25, "window_days": 30},
    ))
    assert cfg.coach.enabled is False
    assert cfg.coach.provider == "anthropic"
    assert cfg.coach.model == "claude-haiku-4-5"
    assert cfg.coach.timeout_seconds == 60
    assert cfg.coach.max_jobs == 25
    assert cfg.coach.window_days == 30


_NOW = datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc)


def _match(job_id="j1", status="applied", score=8, gaps=None, company="Acme",
           source="greenhouse:acme", first_seen="2026-07-01T00:00:00+00:00",
           history=None, title="Backend Engineer") -> dict:
    return {
        "job_id": job_id, "title": title, "company": company,
        "location_text": "Remote (US)", "comp_min": None, "comp_max": None,
        "apply_url": f"https://apply/{job_id}", "source": source,
        "posted_at": None, "score": score, "rationale": None,
        "gaps": gaps or [], "first_seen": first_seen, "status": status,
        "history": history if history is not None else [
            {"status": status, "at": "2026-07-02T00:00:00+00:00"}
        ],
        "posting_closed_at": None, "closed_misses": 0, "closed_notified": False,
        "email_suggestion": None, "dismissed_suggestions": [],
    }


def _content() -> ResumeContent:
    return ResumeContent(
        name="Jane", contact={},
        skills=[Skill(name="Python", category="lang"), Skill(name="Go", category="lang")],
        experiences=[Experience(
            id="exp-a", company="Acme", role="SWE", start="2020", end="2024",
            bullets=[Bullet(id="exp-a-b1", text="Shipped the thing", tags=["backend"],
                            metric_bearing=False)],
        )],
        projects=[Project(
            id="proj-x", name="X",
            bullets=[Bullet(id="proj-x-b1", text="Cut latency 40%", metric_bearing=True)],
        )],
    )


def test_snapshot_funnel_block():
    matches = [_match("j1", status="applied"), _match("j2", status="interviewing"),
               _match("j3", status="new", history=[])]
    snap = build_snapshot(matches=matches, now=_NOW)
    rates = {r["label"]: r for r in snap.funnel["rates"]}
    assert rates["Apply rate"]["num"] == 2 and rates["Apply rate"]["den"] == 3
    assert rates["Interview rate"]["num"] == 1 and rates["Interview rate"]["den"] == 2
    assert snap.funnel["stage_counts"]["matches"] == 3
    assert snap.funnel["stage_counts"]["applied"] == 2


def test_snapshot_jobs_rows_pursued_only_capped_and_sorted():
    matches = [
        _match("j1", status="new", history=[]),          # not pursued
        _match("j2", status="dismissed", history=[]),    # not pursued
        _match("j3", status="applied", first_seen="2026-07-03T00:00:00+00:00"),
        _match("j4", status="interested", first_seen="2026-07-05T00:00:00+00:00"),
        _match("j5", status="rejected", first_seen="2026-07-04T00:00:00+00:00"),
    ]
    snap = build_snapshot(matches=matches, max_jobs=2, now=_NOW)
    assert [j["status"] for j in snap.jobs] == ["interested", "rejected"]  # newest first, capped
    row = snap.jobs[0]
    assert row["source"] == "greenhouse"           # family prefix only
    assert row["days_since_first_seen"] == 7
    assert row["days_in_stage"] == 10              # history 'at' = 2026-07-02


def test_snapshot_job_row_tolerates_malformed_history():
    m = _match("j1", status="applied", history=["garbage", {"no_at": True}])
    snap = build_snapshot(matches=[m], now=_NOW)
    assert snap.jobs[0]["days_in_stage"] == 11     # falls back to first_seen (07-01)


def test_snapshot_gap_frequency_counts_applied_or_beyond_only():
    matches = [
        _match("j1", status="applied", gaps=["Kubernetes", "Kafka"]),
        _match("j2", status="rejected", gaps=["Kubernetes"],
               history=[{"status": "applied", "at": "2026-07-02T00:00:00+00:00"},
                        {"status": "rejected", "at": "2026-07-03T00:00:00+00:00"}]),
        _match("j3", status="interested", gaps=["Terraform"]),  # never applied — excluded
    ]
    snap = build_snapshot(matches=matches, now=_NOW)
    assert snap.aggregates["gap_frequency_applied"] == {"Kubernetes": 2, "Kafka": 1}
    assert snap.aggregates["pursued_by_company"] == {"Acme": 3}
    assert snap.aggregates["pursued_by_source"] == {"greenhouse": 3}


def test_snapshot_meta_low_sample_flag():
    few = [_match(f"j{i}", status="applied") for i in range(3)]
    snap = build_snapshot(matches=few, now=_NOW)
    assert snap.meta["applied_count"] == 3
    assert snap.meta["low_sample"] is True
    many = [_match(f"j{i}", status="applied") for i in range(10)]
    assert build_snapshot(matches=many, now=_NOW).meta["low_sample"] is False


def test_snapshot_resume_bank_shape():
    snap = build_snapshot(matches=[], content=_content(), now=_NOW)
    bank = snap.resume_bank
    assert bank["skills"] == ["Python", "Go"]
    assert bank["experiences"][0]["bullets"][0] == {
        "id": "exp-a-b1", "text": "Shipped the thing",
        "tags": ["backend"], "metric_bearing": False,
    }
    assert bank["projects"][0]["bullets"][0]["metric_bearing"] is True


def test_snapshot_missing_inputs_are_none_and_audit_passthrough():
    audit = {"window_days": 90, "rejected_by_gate": {"role": 5}}
    snap = build_snapshot(matches=[], audit=audit, now=_NOW)
    assert snap.config is None and snap.profile is None and snap.resume_bank is None
    assert snap.aggregates["audit"] == audit
    no_audit = build_snapshot(matches=[], now=_NOW)
    assert "audit" not in no_audit.aggregates


def test_snapshot_to_json_round_trips():
    import json
    snap = build_snapshot(matches=[_match()], config={"filters": {"titles": ["engineer"]}},
                          profile_text="profile", content=_content(), now=_NOW)
    obj = json.loads(snap.to_json())
    assert set(obj) == {"config", "profile", "resume_bank", "funnel", "jobs", "aggregates", "meta"}
    assert obj["meta"]["generated_at"] == "2026-07-12T12:00:00+00:00"


from src.coach import parse_coach_json

_CARD = ('{"category": "filters", "title": "Widen titles", '
         '"evidence": "0 of 12 matches were staff-level", '
         '"action": "add \'staff\' to filters.seniority_allow", "impact": "high"}')


def test_parse_coach_json_happy_path():
    r = parse_coach_json('{"recommendations": [%s]}' % _CARD, provider="ollama")
    assert r.is_fallback is False
    assert len(r.cards) == 1
    c = r.cards[0]
    assert (c.category, c.impact) == ("filters", "high")
    assert c.title == "Widen titles"


def test_parse_coach_json_fenced():
    r = parse_coach_json('```json\n{"recommendations": [%s]}\n```' % _CARD, provider="ollama")
    assert r.is_fallback is False and len(r.cards) == 1


def test_parse_coach_json_drops_malformed_cards_keeps_valid():
    raw = ('{"recommendations": [%s, '
           '{"category": "nonsense", "title": "x", "evidence": "y", "action": "z", "impact": "high"}, '
           '{"category": "resume", "title": "", "evidence": "y", "action": "z", "impact": "low"}, '
           '{"category": "resume", "title": "t", "evidence": "y", "action": "z", "impact": "sideways"}, '
           '"not even a dict"]}' % _CARD)
    r = parse_coach_json(raw, provider="ollama")
    assert r.is_fallback is False
    assert [c.category for c in r.cards] == ["filters"]


def test_parse_coach_json_caps_at_seven():
    cards = ", ".join([_CARD] * 10)
    r = parse_coach_json('{"recommendations": [%s]}' % cards, provider="ollama")
    assert len(r.cards) == 7


def test_parse_coach_json_normalizes_case():
    raw = ('{"recommendations": [{"category": "Filters", "title": "t", '
           '"evidence": "e", "action": "a", "impact": "HIGH"}]}')
    r = parse_coach_json(raw, provider="ollama")
    assert r.cards[0].category == "filters" and r.cards[0].impact == "high"


def test_parse_coach_json_zero_valid_cards_is_error():
    r = parse_coach_json('{"recommendations": []}', provider="ollama")
    assert r.is_fallback is True and r.error_type == "EmptyRecommendations"


def test_parse_coach_json_fallbacks():
    assert parse_coach_json("", provider="ollama").error_type == "MalformedResponse"
    assert parse_coach_json("not json", provider="ollama").error_type == "MalformedResponse"
    assert parse_coach_json('{"other": 1}', provider="ollama").error_type == "MalformedResponse"
    assert parse_coach_json('{"recommendations": "nope"}', provider="ollama").error_type == "MalformedResponse"


import pytest
from unittest.mock import AsyncMock, MagicMock

from src.coach import AnthropicCoach, GeminiCoach, OllamaCoach

_SNAP = build_snapshot(matches=[_match()], now=_NOW)

_REC = {"category": "resume", "title": "Add metrics",
        "evidence": "1 of 2 bullets is metric-bearing", "action": "rewrite exp-a-b1",
        "impact": "medium"}


def _anthropic_coach_response(recs):
    block = MagicMock()
    block.type = "tool_use"
    block.name = "record_recommendations"
    block.input = {"recommendations": recs}
    resp = MagicMock()
    resp.content = [block]
    return resp


@pytest.mark.asyncio
async def test_anthropic_coach_happy_path():
    client = MagicMock()
    client.messages.create = AsyncMock(return_value=_anthropic_coach_response([_REC]))
    engine = AnthropicCoach(client=client, model="claude-haiku-4-5", timeout_seconds=120)
    r = await engine.recommend(_SNAP)
    assert r.is_fallback is False
    assert r.cards[0].title == "Add metrics"
    # forced tool_use, snapshot JSON in the user message
    kwargs = client.messages.create.call_args.kwargs
    assert kwargs["tool_choice"] == {"type": "tool", "name": "record_recommendations"}
    assert '"applied_count"' in kwargs["messages"][0]["content"]


@pytest.mark.asyncio
async def test_anthropic_coach_fallback_on_network_error():
    import httpx
    client = MagicMock()
    client.messages.create = AsyncMock(side_effect=httpx.ConnectError("dns"))
    engine = AnthropicCoach(client=client, model="m", timeout_seconds=120)
    r = await engine.recommend(_SNAP)
    assert r.is_fallback is True and r.cards == [] and r.error_type == "ConnectError"


@pytest.mark.asyncio
async def test_anthropic_coach_fallback_on_missing_tool_use():
    text_block = MagicMock()
    text_block.type = "text"
    resp = MagicMock()
    resp.content = [text_block]
    client = MagicMock()
    client.messages.create = AsyncMock(return_value=resp)
    engine = AnthropicCoach(client=client, model="m", timeout_seconds=120)
    r = await engine.recommend(_SNAP)
    assert r.is_fallback is True and r.error_type == "MalformedResponse"


@pytest.mark.asyncio
async def test_anthropic_coach_fallback_on_non_dict_tool_input():
    block = MagicMock()
    block.type = "tool_use"
    block.name = "record_recommendations"
    block.input = 5  # non-dict scalar: must degrade, not raise
    resp = MagicMock()
    resp.content = [block]
    client = MagicMock()
    client.messages.create = AsyncMock(return_value=resp)
    engine = AnthropicCoach(client=client, model="m", timeout_seconds=120)
    r = await engine.recommend(_SNAP)
    assert r.is_fallback is True and r.error_type == "MalformedResponse"


@pytest.mark.asyncio
async def test_anthropic_coach_zero_cards_is_error():
    client = MagicMock()
    client.messages.create = AsyncMock(return_value=_anthropic_coach_response([]))
    engine = AnthropicCoach(client=client, model="m", timeout_seconds=120)
    r = await engine.recommend(_SNAP)
    assert r.is_fallback is True and r.error_type == "EmptyRecommendations"


@pytest.mark.asyncio
async def test_gemini_coach_happy_path():
    import json as _json
    resp = MagicMock()
    resp.text = _json.dumps({"recommendations": [_REC]})
    client = MagicMock()
    client.aio.models.generate_content = AsyncMock(return_value=resp)
    engine = GeminiCoach(client=client, model="gemini-2.5-flash", timeout_seconds=120)
    r = await engine.recommend(_SNAP)
    assert r.is_fallback is False and r.cards[0].category == "resume"


@pytest.mark.asyncio
async def test_gemini_coach_fallback_on_error():
    client = MagicMock()
    client.aio.models.generate_content = AsyncMock(side_effect=RuntimeError("boom"))
    engine = GeminiCoach(client=client, model="m", timeout_seconds=120)
    r = await engine.recommend(_SNAP)
    assert r.is_fallback is True and r.error_type == "RuntimeError"


@pytest.mark.asyncio
async def test_ollama_coach_happy_path():
    import json as _json
    client = MagicMock()
    client.chat = AsyncMock(return_value={"message": {"content": _json.dumps({"recommendations": [_REC]})}})
    engine = OllamaCoach(client=client, model="gpt-oss:120b", timeout_seconds=120)
    r = await engine.recommend(_SNAP)
    assert r.is_fallback is False and len(r.cards) == 1
    # system prompt carries the JSON output hint for schema-free Ollama
    messages = client.chat.call_args.kwargs["messages"]
    assert "ONLY a JSON object" in messages[0]["content"]


@pytest.mark.asyncio
async def test_ollama_coach_fallback_on_malformed():
    client = MagicMock()
    client.chat = AsyncMock(return_value={"message": {"content": "no json here"}})
    engine = OllamaCoach(client=client, model="m", timeout_seconds=120)
    r = await engine.recommend(_SNAP)
    assert r.is_fallback is True and r.error_type == "MalformedResponse"


from src.handler import _build_coach


def test_build_coach_disabled_returns_none():
    cfg = AppConfig.model_validate(_raw_config(coach={"enabled": False}))
    assert _build_coach(cfg) is None


def test_build_coach_missing_key_returns_none():
    # provider follows relevance (ollama, cloud host) but no ollama_api_key is set
    cfg = AppConfig.model_validate(_raw_config())
    assert _build_coach(cfg) is None


def test_build_coach_missing_anthropic_key_returns_none():
    cfg = AppConfig.model_validate(_raw_config(coach={"provider": "anthropic"}))
    assert _build_coach(cfg) is None


def test_build_coach_local_ollama_needs_no_key(monkeypatch):
    pytest.importorskip("ollama")
    monkeypatch.setenv("JOB_AGG_OLLAMA_HOST", "http://localhost:11434")
    cfg = AppConfig.model_validate(_raw_config())
    engine = _build_coach(cfg)
    assert engine is not None
    assert type(engine).__name__ == "OllamaCoach"
