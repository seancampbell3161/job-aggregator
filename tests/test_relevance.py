import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.models import NormalizedPosting
from src.relevance import RelevanceScorer, Score


def _posting(**overrides) -> NormalizedPosting:
    base = dict(
        job_id="greenhouse:stripe:1",
        title="Senior Backend Engineer",
        company="Stripe",
        location_text="Remote, United States",
        location_tags=frozenset({"remote", "us"}),
        seniority="senior",
        stack=frozenset({"python", "go"}),
        comp_min=180_000,
        comp_max=240_000,
        apply_url="https://example.com/apply",
        description="Build payments infra in Python and Go.",
        posted_at=None,
        source="greenhouse:stripe",
    )
    base.update(overrides)
    return NormalizedPosting(**base)


def _make_anthropic_response(*, score: int = 8, rationale: str = "Strong fit"):
    """Construct a mocked Anthropic Messages response that returns a tool_use
    block with the given score + rationale."""
    block = MagicMock()
    block.type = "tool_use"
    block.name = "record_relevance"
    block.input = {"score": score, "rationale": rationale}
    resp = MagicMock()
    resp.content = [block]
    return resp


@pytest.mark.asyncio
async def test_score_happy_path_parses_tool_use():
    """A valid tool_use response is unpacked into a Score."""
    client = MagicMock()
    client.messages.create = AsyncMock(return_value=_make_anthropic_response(score=8, rationale="Solid fit"))

    scorer = RelevanceScorer(
        client=client,
        model="claude-haiku-4-5",
        profile_md="# profile",
        timeout_seconds=10,
    )
    result = await scorer.score(_posting())

    assert result.value == 8
    assert result.rationale == "Solid fit"
    assert result.is_fallback is False


@pytest.mark.asyncio
async def test_score_returns_fallback_on_network_error():
    """Network errors return a fail-open Score, never raise."""
    import httpx

    client = MagicMock()
    client.messages.create = AsyncMock(side_effect=httpx.ConnectError("dns"))

    scorer = RelevanceScorer(
        client=client, model="claude-haiku-4-5", profile_md="x", timeout_seconds=10,
    )
    result = await scorer.score(_posting())

    assert result.value is None
    assert result.is_fallback is True
    assert "unavailable" in result.rationale.lower()


@pytest.mark.asyncio
async def test_score_returns_fallback_on_rate_limit():
    """Anthropic 429 → fail-open."""
    from anthropic import RateLimitError

    client = MagicMock()
    err = RateLimitError(
        message="rate limit",
        response=MagicMock(status_code=429, headers={}),
        body=None,
    )
    client.messages.create = AsyncMock(side_effect=err)

    scorer = RelevanceScorer(
        client=client, model="claude-haiku-4-5", profile_md="x", timeout_seconds=10,
    )
    result = await scorer.score(_posting())

    assert result.is_fallback is True


@pytest.mark.asyncio
async def test_score_returns_fallback_on_missing_tool_use():
    """If the model returns text instead of a tool_use, fail-open."""
    text_block = MagicMock()
    text_block.type = "text"
    text_block.text = "I refuse"
    resp = MagicMock()
    resp.content = [text_block]

    client = MagicMock()
    client.messages.create = AsyncMock(return_value=resp)

    scorer = RelevanceScorer(
        client=client, model="claude-haiku-4-5", profile_md="x", timeout_seconds=10,
    )
    result = await scorer.score(_posting())

    assert result.is_fallback is True


@pytest.mark.asyncio
async def test_score_clips_out_of_range_value():
    """Score >10 or <0 gets clipped, not failed."""
    client = MagicMock()
    client.messages.create = AsyncMock(return_value=_make_anthropic_response(score=42, rationale="x"))

    scorer = RelevanceScorer(
        client=client, model="claude-haiku-4-5", profile_md="x", timeout_seconds=10,
    )
    result = await scorer.score(_posting())

    assert result.value == 10
    assert result.is_fallback is False


@pytest.mark.asyncio
async def test_score_returns_fallback_on_missing_score_field():
    """A tool_use with missing required fields → fail-open."""
    block = MagicMock()
    block.type = "tool_use"
    block.name = "record_relevance"
    block.input = {"rationale": "no score"}  # missing 'score'
    resp = MagicMock()
    resp.content = [block]

    client = MagicMock()
    client.messages.create = AsyncMock(return_value=resp)

    scorer = RelevanceScorer(
        client=client, model="claude-haiku-4-5", profile_md="x", timeout_seconds=10,
    )
    result = await scorer.score(_posting())

    assert result.is_fallback is True


@pytest.mark.asyncio
async def test_score_marks_system_block_for_caching():
    """The system block (instructions + profile) carries cache_control: ephemeral
    so Anthropic prompt caching kicks in across postings in a cycle."""
    captured: dict = {}

    async def fake_create(**kwargs):
        captured.update(kwargs)
        return _make_anthropic_response()

    client = MagicMock()
    client.messages.create = fake_create

    scorer = RelevanceScorer(
        client=client, model="claude-haiku-4-5", profile_md="# my profile content", timeout_seconds=10,
    )
    await scorer.score(_posting())

    sys_blocks = captured["system"]
    assert isinstance(sys_blocks, list)
    last = sys_blocks[-1]
    assert last.get("cache_control") == {"type": "ephemeral"}
    assert "my profile content" in last["text"]


@pytest.mark.asyncio
async def test_score_forces_record_relevance_tool():
    """The Messages call must force the record_relevance tool so the model
    can't return free-form text."""
    captured: dict = {}

    async def fake_create(**kwargs):
        captured.update(kwargs)
        return _make_anthropic_response()

    client = MagicMock()
    client.messages.create = fake_create

    scorer = RelevanceScorer(
        client=client, model="claude-haiku-4-5", profile_md="x", timeout_seconds=10,
    )
    await scorer.score(_posting())

    assert captured["tool_choice"] == {"type": "tool", "name": "record_relevance"}
    tools = captured["tools"]
    assert any(t["name"] == "record_relevance" for t in tools)


@pytest.mark.asyncio
async def test_score_includes_posting_summary_in_user_message():
    """The user-message content includes the title, company, stack, location."""
    captured: dict = {}

    async def fake_create(**kwargs):
        captured.update(kwargs)
        return _make_anthropic_response()

    client = MagicMock()
    client.messages.create = fake_create

    p = _posting(title="Senior Backend Engineer", company="Stripe")
    scorer = RelevanceScorer(
        client=client, model="claude-haiku-4-5", profile_md="x", timeout_seconds=10,
    )
    await scorer.score(p)

    user_msgs = [m for m in captured["messages"] if m["role"] == "user"]
    assert user_msgs
    user_text = user_msgs[0]["content"]
    assert "Senior Backend Engineer" in user_text
    assert "Stripe" in user_text


@pytest.mark.asyncio
async def test_score_truncates_long_descriptions():
    """Descriptions are capped at _MAX_DESCRIPTION_CHARS to bound input cost."""
    from src.relevance import _MAX_DESCRIPTION_CHARS

    captured: dict = {}

    async def fake_create(**kwargs):
        captured.update(kwargs)
        return _make_anthropic_response()

    client = MagicMock()
    client.messages.create = fake_create

    big = "x" * (_MAX_DESCRIPTION_CHARS + 3000)
    p = _posting(description=big)
    scorer = RelevanceScorer(
        client=client, model="claude-haiku-4-5", profile_md="x", timeout_seconds=10,
    )
    await scorer.score(p)

    user_text = [m for m in captured["messages"] if m["role"] == "user"][0]["content"]
    # Description portion is capped; check the user message length is bounded.
    assert "x" * _MAX_DESCRIPTION_CHARS in user_text
    assert "x" * (_MAX_DESCRIPTION_CHARS + 1) not in user_text


@pytest.mark.asyncio
async def test_score_returns_fallback_on_timeout():
    """asyncio.TimeoutError (from Anthropic SDK timeout) must fail-open like
    every other error class. Locks the timeout branch of the fail-open contract."""
    import asyncio

    client = MagicMock()
    client.messages.create = AsyncMock(side_effect=asyncio.TimeoutError())

    scorer = RelevanceScorer(
        client=client, model="claude-haiku-4-5", profile_md="x", timeout_seconds=10,
    )
    result = await scorer.score(_posting())

    assert result.value is None
    assert result.is_fallback is True
    assert "unavailable" in result.rationale.lower()


# --------------------------------------------------------------------------- #
# GeminiRelevanceScorer — parallel test set for the Google Gen AI provider.
# Mocks the SDK's async generate_content path. Same Score contract,
# same fail-open semantics.
# --------------------------------------------------------------------------- #


def _make_gemini_response(*, score: int = 8, rationale: str = "Strong fit"):
    """Construct a mocked google.genai response whose .text is a JSON string
    matching the relevance schema."""
    import json
    resp = MagicMock()
    resp.text = json.dumps({"score": score, "rationale": rationale})
    return resp


def _gemini_client_returning(resp):
    """Build a MagicMock that mimics genai.Client's shape: client.aio.models.generate_content."""
    client = MagicMock()
    client.aio = MagicMock()
    client.aio.models = MagicMock()
    client.aio.models.generate_content = AsyncMock(return_value=resp)
    return client


def _gemini_client_raising(exc):
    client = MagicMock()
    client.aio = MagicMock()
    client.aio.models = MagicMock()
    client.aio.models.generate_content = AsyncMock(side_effect=exc)
    return client


@pytest.mark.asyncio
async def test_gemini_score_happy_path_parses_json():
    from src.relevance import GeminiRelevanceScorer

    client = _gemini_client_returning(_make_gemini_response(score=9, rationale="Excellent fit"))
    scorer = GeminiRelevanceScorer(
        client=client, model="gemini-2.0-flash", profile_md="# profile", timeout_seconds=10,
    )
    result = await scorer.score(_posting())

    assert result.value == 9
    assert result.rationale == "Excellent fit"
    assert result.is_fallback is False


@pytest.mark.asyncio
async def test_gemini_score_returns_fallback_on_network_error():
    import httpx
    from src.relevance import GeminiRelevanceScorer

    client = _gemini_client_raising(httpx.ConnectError("dns"))
    scorer = GeminiRelevanceScorer(
        client=client, model="gemini-2.0-flash", profile_md="x", timeout_seconds=10,
    )
    result = await scorer.score(_posting())

    assert result.value is None
    assert result.is_fallback is True
    assert "unavailable" in result.rationale.lower()


@pytest.mark.asyncio
async def test_gemini_score_returns_fallback_on_api_error():
    """genai errors.APIError (e.g. 429 rate limit) must fail-open."""
    from src.relevance import GeminiRelevanceScorer

    # Construct without importing genai (which isn't installed in test env yet).
    # The scorer's except clause is broad (Exception); any error class works.
    class FakeAPIError(Exception):
        code = 429

    client = _gemini_client_raising(FakeAPIError("rate limited"))
    scorer = GeminiRelevanceScorer(
        client=client, model="gemini-2.0-flash", profile_md="x", timeout_seconds=10,
    )
    result = await scorer.score(_posting())

    assert result.is_fallback is True


@pytest.mark.asyncio
async def test_gemini_score_returns_fallback_on_malformed_json():
    from src.relevance import GeminiRelevanceScorer

    resp = MagicMock()
    resp.text = "this is not json {"
    client = _gemini_client_returning(resp)
    scorer = GeminiRelevanceScorer(
        client=client, model="gemini-2.0-flash", profile_md="x", timeout_seconds=10,
    )
    result = await scorer.score(_posting())

    assert result.is_fallback is True


@pytest.mark.asyncio
async def test_gemini_score_returns_fallback_on_missing_score_field():
    """A JSON object without 'score' is treated as malformed → fail-open."""
    import json
    from src.relevance import GeminiRelevanceScorer

    resp = MagicMock()
    resp.text = json.dumps({"rationale": "no score field"})
    client = _gemini_client_returning(resp)
    scorer = GeminiRelevanceScorer(
        client=client, model="gemini-2.0-flash", profile_md="x", timeout_seconds=10,
    )
    result = await scorer.score(_posting())

    assert result.is_fallback is True


@pytest.mark.asyncio
async def test_gemini_score_clips_out_of_range_value():
    from src.relevance import GeminiRelevanceScorer

    client = _gemini_client_returning(_make_gemini_response(score=99, rationale="x"))
    scorer = GeminiRelevanceScorer(
        client=client, model="gemini-2.0-flash", profile_md="x", timeout_seconds=10,
    )
    result = await scorer.score(_posting())

    assert result.value == 10
    assert result.is_fallback is False


@pytest.mark.asyncio
async def test_gemini_score_truncates_long_descriptions():
    """Descriptions are capped at _MAX_DESCRIPTION_CHARS to bound input cost."""
    captured: dict = {}

    async def fake_generate(**kwargs):
        captured.update(kwargs)
        return _make_gemini_response()

    client = MagicMock()
    client.aio = MagicMock()
    client.aio.models = MagicMock()
    client.aio.models.generate_content = fake_generate

    from src.relevance import GeminiRelevanceScorer, _MAX_DESCRIPTION_CHARS
    big = "x" * (_MAX_DESCRIPTION_CHARS + 3000)
    p = _posting(description=big)
    scorer = GeminiRelevanceScorer(
        client=client, model="gemini-2.0-flash", profile_md="x", timeout_seconds=10,
    )
    await scorer.score(p)

    contents = captured["contents"]
    assert "x" * _MAX_DESCRIPTION_CHARS in contents
    assert "x" * (_MAX_DESCRIPTION_CHARS + 1) not in contents


@pytest.mark.asyncio
async def test_gemini_score_passes_system_instruction_with_profile():
    """The profile text must appear in the Gemini system_instruction so the
    model has the candidate context."""
    captured: dict = {}

    async def fake_generate(**kwargs):
        captured.update(kwargs)
        return _make_gemini_response()

    client = MagicMock()
    client.aio = MagicMock()
    client.aio.models = MagicMock()
    client.aio.models.generate_content = fake_generate

    from src.relevance import GeminiRelevanceScorer
    scorer = GeminiRelevanceScorer(
        client=client, model="gemini-2.0-flash", profile_md="# my profile content", timeout_seconds=10,
    )
    await scorer.score(_posting())

    cfg = captured["config"]
    # The config may be a types.GenerateContentConfig or a dict; check both.
    sys_text = getattr(cfg, "system_instruction", None) or (cfg.get("system_instruction") if isinstance(cfg, dict) else None)
    assert sys_text is not None
    assert "my profile content" in sys_text


@pytest.mark.asyncio
async def test_gemini_score_requests_json_response_format():
    """The Gemini call forces JSON output via response_mime_type + schema."""
    captured: dict = {}

    async def fake_generate(**kwargs):
        captured.update(kwargs)
        return _make_gemini_response()

    client = MagicMock()
    client.aio = MagicMock()
    client.aio.models = MagicMock()
    client.aio.models.generate_content = fake_generate

    from src.relevance import GeminiRelevanceScorer
    scorer = GeminiRelevanceScorer(
        client=client, model="gemini-2.0-flash", profile_md="x", timeout_seconds=10,
    )
    await scorer.score(_posting())

    cfg = captured["config"]
    mime = getattr(cfg, "response_mime_type", None) or (cfg.get("response_mime_type") if isinstance(cfg, dict) else None)
    schema = getattr(cfg, "response_json_schema", None) or (cfg.get("response_json_schema") if isinstance(cfg, dict) else None)
    assert mime == "application/json"
    assert schema is not None
    # Schema requires score + rationale
    props = schema["properties"] if isinstance(schema, dict) else schema.properties
    assert "score" in props
    assert "rationale" in props


def test_parse_score_json_plain_object():
    from src.relevance import _parse_score_json
    s = _parse_score_json('{"score": 7, "rationale": "ok"}', job_id="j", provider="ollama")
    assert s.value == 7
    assert s.rationale == "ok"
    assert s.is_fallback is False


def test_parse_score_json_strips_markdown_fences():
    from src.relevance import _parse_score_json
    raw = '```json\n{"score": 5, "rationale": "fenced"}\n```'
    s = _parse_score_json(raw, job_id="j", provider="ollama")
    assert s.value == 5
    assert s.rationale == "fenced"
    assert s.is_fallback is False


def test_parse_score_json_extracts_from_surrounding_prose():
    from src.relevance import _parse_score_json
    raw = 'Here is my assessment: {"score": 6, "rationale": "prose"} — done.'
    s = _parse_score_json(raw, job_id="j", provider="ollama")
    assert s.value == 6
    assert s.is_fallback is False


def test_parse_score_json_clamps_and_defaults_rationale():
    from src.relevance import _parse_score_json
    s = _parse_score_json('{"score": 42}', job_id="j", provider="ollama")
    assert s.value == 10
    assert s.rationale == "(no rationale)"
    assert s.is_fallback is False


def test_parse_score_json_fallback_on_empty():
    from src.relevance import _parse_score_json
    s = _parse_score_json("", job_id="j", provider="ollama")
    assert s.value is None
    assert s.is_fallback is True


def test_parse_score_json_fallback_on_non_json():
    from src.relevance import _parse_score_json
    s = _parse_score_json("totally not json", job_id="j", provider="ollama")
    assert s.is_fallback is True


def test_parse_score_json_fallback_on_missing_score():
    from src.relevance import _parse_score_json
    s = _parse_score_json('{"rationale": "no score"}', job_id="j", provider="ollama")
    assert s.is_fallback is True


@pytest.mark.asyncio
async def test_gemini_score_returns_fallback_on_timeout():
    """asyncio.TimeoutError from a wait_for wrapping the SDK call must fail-open."""
    import asyncio
    from src.relevance import GeminiRelevanceScorer

    async def slow_generate(**kwargs):
        await asyncio.sleep(10)
        return _make_gemini_response()

    client = MagicMock()
    client.aio = MagicMock()
    client.aio.models = MagicMock()
    client.aio.models.generate_content = slow_generate

    scorer = GeminiRelevanceScorer(
        client=client, model="gemini-2.0-flash", profile_md="x", timeout_seconds=0,
    )
    result = await scorer.score(_posting())

    assert result.value is None
    assert result.is_fallback is True
    assert "unavailable" in result.rationale.lower()


# --------------------------------------------------------------------------- #
# OllamaRelevanceScorer — Ollama Cloud provider. Mocks the async .chat() path.
# Cloud does not enforce JSON schemas, so output is prompt-engineered JSON
# parsed by the shared _parse_score_json. Same fail-open contract.
# --------------------------------------------------------------------------- #


def _make_ollama_response(*, score: int = 8, rationale: str = "Strong fit", content=None):
    """Mock an ollama ChatResponse whose .message.content holds the text."""
    import json
    if content is None:
        content = json.dumps({"score": score, "rationale": rationale})
    msg = MagicMock()
    msg.content = content
    resp = MagicMock()
    resp.message = msg
    return resp


def _ollama_client_returning(resp):
    client = MagicMock()
    client.chat = AsyncMock(return_value=resp)
    return client


def _ollama_client_raising(exc):
    client = MagicMock()
    client.chat = AsyncMock(side_effect=exc)
    return client


@pytest.mark.asyncio
async def test_ollama_score_happy_path_parses_json():
    from src.relevance import OllamaRelevanceScorer

    client = _ollama_client_returning(_make_ollama_response(score=9, rationale="Excellent fit"))
    scorer = OllamaRelevanceScorer(
        client=client, model="gpt-oss:20b", profile_md="# profile", timeout_seconds=10,
    )
    result = await scorer.score(_posting())

    assert result.value == 9
    assert result.rationale == "Excellent fit"
    assert result.is_fallback is False


@pytest.mark.asyncio
async def test_ollama_score_parses_fenced_json():
    from src.relevance import OllamaRelevanceScorer

    resp = _make_ollama_response(content='```json\n{"score": 6, "rationale": "fenced"}\n```')
    scorer = OllamaRelevanceScorer(
        client=_ollama_client_returning(resp), model="gpt-oss:20b", profile_md="x", timeout_seconds=10,
    )
    result = await scorer.score(_posting())
    assert result.value == 6
    assert result.is_fallback is False


@pytest.mark.asyncio
async def test_ollama_score_parses_json_in_prose():
    from src.relevance import OllamaRelevanceScorer

    resp = _make_ollama_response(content='Sure: {"score": 4, "rationale": "meh"} hope that helps')
    scorer = OllamaRelevanceScorer(
        client=_ollama_client_returning(resp), model="gpt-oss:20b", profile_md="x", timeout_seconds=10,
    )
    result = await scorer.score(_posting())
    assert result.value == 4
    assert result.is_fallback is False


@pytest.mark.asyncio
async def test_ollama_score_fallback_on_empty_content():
    from src.relevance import OllamaRelevanceScorer

    resp = _make_ollama_response(content="")
    scorer = OllamaRelevanceScorer(
        client=_ollama_client_returning(resp), model="gpt-oss:20b", profile_md="x", timeout_seconds=10,
    )
    result = await scorer.score(_posting())
    assert result.value is None
    assert result.is_fallback is True


@pytest.mark.asyncio
async def test_ollama_score_fallback_on_network_error():
    import httpx
    from src.relevance import OllamaRelevanceScorer

    client = _ollama_client_raising(httpx.ConnectError("dns"))
    scorer = OllamaRelevanceScorer(
        client=client, model="gpt-oss:20b", profile_md="x", timeout_seconds=10,
    )
    result = await scorer.score(_posting())
    assert result.value is None
    assert result.is_fallback is True
    assert "unavailable" in result.rationale.lower()


@pytest.mark.asyncio
async def test_ollama_score_clips_out_of_range_value():
    from src.relevance import OllamaRelevanceScorer

    client = _ollama_client_returning(_make_ollama_response(score=99, rationale="x"))
    scorer = OllamaRelevanceScorer(
        client=client, model="gpt-oss:20b", profile_md="x", timeout_seconds=10,
    )
    result = await scorer.score(_posting())
    assert result.value == 10
    assert result.is_fallback is False


@pytest.mark.asyncio
async def test_ollama_score_fallback_on_timeout():
    import asyncio
    from src.relevance import OllamaRelevanceScorer

    async def slow_chat(**kwargs):
        await asyncio.sleep(10)
        return _make_ollama_response()

    client = MagicMock()
    client.chat = slow_chat
    scorer = OllamaRelevanceScorer(
        client=client, model="gpt-oss:20b", profile_md="x", timeout_seconds=0,
    )
    result = await scorer.score(_posting())
    assert result.value is None
    assert result.is_fallback is True


@pytest.mark.asyncio
async def test_ollama_score_request_shape():
    """The chat call disables thinking, forces temperature 0, includes the
    profile in the system message, and asks for JSON-only output."""
    captured: dict = {}

    async def fake_chat(**kwargs):
        captured.update(kwargs)
        return _make_ollama_response()

    client = MagicMock()
    client.chat = fake_chat
    from src.relevance import OllamaRelevanceScorer
    scorer = OllamaRelevanceScorer(
        client=client, model="gpt-oss:20b", profile_md="# my profile content", timeout_seconds=10,
    )
    await scorer.score(_posting())

    assert captured["model"] == "gpt-oss:20b"
    assert captured["think"] is False
    assert captured["options"]["temperature"] == 0
    # Reasoning models burn tokens on chain-of-thought before the answer, so the
    # generation budget must have headroom (a 200 cap left content empty).
    assert captured["options"]["num_predict"] >= 512
    sys_msg = [m for m in captured["messages"] if m["role"] == "system"][0]["content"]
    assert "my profile content" in sys_msg
    assert "JSON" in sys_msg
    user_msg = [m for m in captured["messages"] if m["role"] == "user"][0]["content"]
    assert "Stripe" in user_msg


def test_parse_score_json_fallback_on_non_string_input():
    """Non-str truthy input (e.g. a dict) must fail open, never raise."""
    from src.relevance import _parse_score_json
    s = _parse_score_json({"score": 8}, job_id="j", provider="ollama")
    assert s.value is None
    assert s.is_fallback is True


@pytest.mark.asyncio
async def test_ollama_score_fallback_on_non_string_content():
    """If the SDK hands back non-str message content, score() fails open
    rather than letting AttributeError escape."""
    from src.relevance import OllamaRelevanceScorer

    resp = _make_ollama_response(content={"score": 8})  # dict, not str
    scorer = OllamaRelevanceScorer(
        client=_ollama_client_returning(resp), model="gpt-oss:20b", profile_md="x", timeout_seconds=10,
    )
    result = await scorer.score(_posting())
    assert result.value is None
    assert result.is_fallback is True


@pytest.mark.asyncio
async def test_gemini_score_fallback_on_non_string_text():
    """Non-str resp.text must fail open, not raise."""
    from src.relevance import GeminiRelevanceScorer

    resp = MagicMock()
    resp.text = 123  # non-str truthy
    client = _gemini_client_returning(resp)
    scorer = GeminiRelevanceScorer(
        client=client, model="gemini-2.0-flash", profile_md="x", timeout_seconds=10,
    )
    result = await scorer.score(_posting())
    assert result.value is None
    assert result.is_fallback is True


# --------------------------------------------------------------------------- #
# The fail-open warning must record the exception *class*, not just str(exc).
# A wait_for timeout's str() is empty (str(TimeoutError()) == ""), so the
# 2026-06-15 Ollama incident logged `error: ""` and the cause was unguessable.
# error_type carries the signal no matter how chatty str(exc) is.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_ollama_call_failed_log_records_exception_type(caplog):
    """A wait_for timeout has an empty str(), so the Ollama fail-open warning
    must also record the exception class name to stay diagnosable."""
    import asyncio
    import logging
    from src.relevance import OllamaRelevanceScorer

    async def slow_chat(**kwargs):
        await asyncio.sleep(10)
        return _make_ollama_response()

    client = MagicMock()
    client.chat = slow_chat
    scorer = OllamaRelevanceScorer(
        client=client, model="gpt-oss:120b", profile_md="x", timeout_seconds=0,
    )
    with caplog.at_level(logging.WARNING, logger="src.relevance"):
        result = await scorer.score(_posting())

    assert result.is_fallback is True
    rec = next(r for r in caplog.records if r.msg == "relevance_llm_call_failed")
    assert rec.error == ""                                      # str(TimeoutError()) is blank...
    assert getattr(rec, "error_type", None) == "TimeoutError"   # ...so error_type carries it


@pytest.mark.asyncio
async def test_gemini_call_failed_log_records_exception_type(caplog):
    """Gemini fail-open warning records the exception class name too."""
    import asyncio
    import logging
    from src.relevance import GeminiRelevanceScorer

    async def slow_generate(**kwargs):
        await asyncio.sleep(10)
        return _make_gemini_response()

    client = MagicMock()
    client.aio = MagicMock()
    client.aio.models = MagicMock()
    client.aio.models.generate_content = slow_generate
    scorer = GeminiRelevanceScorer(
        client=client, model="gemini-2.0-flash", profile_md="x", timeout_seconds=0,
    )
    with caplog.at_level(logging.WARNING, logger="src.relevance"):
        result = await scorer.score(_posting())

    assert result.is_fallback is True
    rec = next(r for r in caplog.records if r.msg == "relevance_llm_call_failed")
    assert getattr(rec, "error_type", None) == "TimeoutError"


@pytest.mark.asyncio
async def test_anthropic_call_failed_log_records_exception_type(caplog):
    """Anthropic fail-open warning records the exception class name too."""
    import asyncio
    import logging

    client = MagicMock()
    client.messages.create = AsyncMock(side_effect=asyncio.TimeoutError())
    scorer = RelevanceScorer(
        client=client, model="claude-haiku-4-5", profile_md="x", timeout_seconds=10,
    )
    with caplog.at_level(logging.WARNING, logger="src.relevance"):
        result = await scorer.score(_posting())

    assert result.is_fallback is True
    rec = next(r for r in caplog.records if r.msg == "relevance_llm_call_failed")
    assert getattr(rec, "error_type", None) == "TimeoutError"


# --------------------------------------------------------------------------- #
# Score.error_type — exception class name or "MalformedResponse" on fallback,
# None on success. Lets the /pipeline dashboard break down LLM degradation.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_score_fallback_carries_exception_error_type():
    """A network error fallback records the exception class name."""
    import httpx
    client = MagicMock()
    client.messages.create = AsyncMock(side_effect=httpx.ConnectError("dns"))
    scorer = RelevanceScorer(client=client, model="claude-haiku-4-5", profile_md="x", timeout_seconds=10)
    result = await scorer.score(_posting())
    assert result.is_fallback is True
    assert result.error_type == "ConnectError"


@pytest.mark.asyncio
async def test_score_fallback_timeout_error_type_survives_empty_str():
    """A timeout exception has an empty str(); error_type must still carry the class name."""
    assert str(TimeoutError()) == ""  # documents why error_type (not str(exc)) is the signal
    client = MagicMock()
    client.messages.create = AsyncMock(side_effect=TimeoutError())
    scorer = RelevanceScorer(client=client, model="claude-haiku-4-5", profile_md="x", timeout_seconds=10)
    result = await scorer.score(_posting())
    assert result.is_fallback is True
    assert result.error_type == "TimeoutError"


@pytest.mark.asyncio
async def test_score_malformed_response_error_type():
    """A tool_use missing the score field → error_type 'MalformedResponse'."""
    block = MagicMock()
    block.type = "tool_use"; block.name = "record_relevance"; block.input = {"rationale": "no score"}
    resp = MagicMock(); resp.content = [block]
    client = MagicMock()
    client.messages.create = AsyncMock(return_value=resp)
    scorer = RelevanceScorer(client=client, model="claude-haiku-4-5", profile_md="x", timeout_seconds=10)
    result = await scorer.score(_posting())
    assert result.is_fallback is True
    assert result.error_type == "MalformedResponse"


@pytest.mark.asyncio
async def test_score_success_has_no_error_type():
    client = MagicMock()
    client.messages.create = AsyncMock(return_value=_make_anthropic_response(score=8, rationale="Solid fit"))
    scorer = RelevanceScorer(client=client, model="claude-haiku-4-5", profile_md="x", timeout_seconds=10)
    result = await scorer.score(_posting())
    assert result.is_fallback is False
    assert result.error_type is None


# --------------------------------------------------------------------------- #
# Ollama transient-failure retry. Cloud capacity 5xx/hangs arrive in bursts
# (2026-08-03: 6 of 8 notifications went out unscored as "[?/10]"), while a
# healthy call answers in seconds — so a bounded retry recovers the score.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_ollama_score_retries_transient_failure_then_succeeds():
    import httpx
    from src.relevance import OllamaRelevanceScorer

    client = MagicMock()
    client.chat = AsyncMock(side_effect=[
        httpx.ConnectError("capacity"),
        _make_ollama_response(score=7, rationale="recovered"),
    ])
    scorer = OllamaRelevanceScorer(
        client=client, model="gpt-oss:20b", profile_md="x", timeout_seconds=10,
    )
    result = await scorer.score(_posting())

    assert result.value == 7
    assert result.rationale == "recovered"
    assert result.is_fallback is False          # no "[?/10]" for a recoverable blip
    assert client.chat.await_count == 2


@pytest.mark.asyncio
async def test_ollama_score_retries_timeout_then_succeeds():
    """asyncio.wait_for raises TimeoutError — the exact error_type recorded in
    llm_failures during the 2026-08-02/03 Ollama Cloud degradation."""
    from src.relevance import OllamaRelevanceScorer

    client = MagicMock()
    client.chat = AsyncMock(side_effect=[
        TimeoutError(),
        _make_ollama_response(score=5, rationale="second try"),
    ])
    scorer = OllamaRelevanceScorer(
        client=client, model="gpt-oss:20b", profile_md="x", timeout_seconds=10,
    )
    result = await scorer.score(_posting())

    assert result.value == 5
    assert result.is_fallback is False
    assert client.chat.await_count == 2


@pytest.mark.asyncio
async def test_ollama_score_gives_up_after_max_attempts():
    """Retry is bounded and still fails open — exactly one fallback per posting,
    so llm_failures keeps counting unscored postings, not attempts."""
    import httpx
    from src.relevance import OllamaRelevanceScorer, _OLLAMA_MAX_ATTEMPTS

    client = _ollama_client_raising(httpx.ConnectError("down"))
    scorer = OllamaRelevanceScorer(
        client=client, model="gpt-oss:20b", profile_md="x", timeout_seconds=10,
    )
    result = await scorer.score(_posting())

    assert result.value is None
    assert result.is_fallback is True
    assert result.error_type == "ConnectError"
    assert client.chat.await_count == _OLLAMA_MAX_ATTEMPTS


@pytest.mark.asyncio
async def test_ollama_score_does_not_retry_a_malformed_success():
    """A 200 carrying unparseable content is a model-behaviour problem, not a
    transient one — retrying would triple cost for the same answer."""
    from src.relevance import OllamaRelevanceScorer

    client = _ollama_client_returning(_make_ollama_response(content="not json"))
    scorer = OllamaRelevanceScorer(
        client=client, model="gpt-oss:20b", profile_md="x", timeout_seconds=10,
    )
    result = await scorer.score(_posting())

    assert result.is_fallback is True
    assert client.chat.await_count == 1


@pytest.mark.asyncio
async def test_score_is_bounded_by_timeout_seconds():
    """The SDK's ``timeout=`` is per attempt and it retries twice by default,
    so only an outer wait_for keeps a hung call from stalling the poll cycle
    for ~3x the budget."""
    async def slow(**kwargs):
        await asyncio.sleep(30)

    client = MagicMock()
    client.messages.create = slow
    scorer = RelevanceScorer(
        client=client, model="claude-haiku-4-5", profile_md="x", timeout_seconds=0.05,
    )
    result = await asyncio.wait_for(scorer.score(_posting()), timeout=5)
    assert result.is_fallback is True
