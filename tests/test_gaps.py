from src.gaps import Gaps, _parse_missing_skills


def test_parse_missing_skills_plain_object():
    g = _parse_missing_skills(
        '{"missing_skills": ["Kubernetes", "Kafka"]}',
        job_id="j", provider="ollama", max_skills=6,
    )
    assert g.skills == ["Kubernetes", "Kafka"]
    assert g.is_fallback is False


def test_parse_missing_skills_empty_list_is_clean_not_fallback():
    g = _parse_missing_skills('{"missing_skills": []}', job_id="j", provider="ollama", max_skills=6)
    assert g.skills == []
    assert g.is_fallback is False


def test_parse_missing_skills_truncates_to_max():
    raw = '{"missing_skills": ["a","b","c","d","e","f","g","h"]}'
    g = _parse_missing_skills(raw, job_id="j", provider="ollama", max_skills=3)
    assert g.skills == ["a", "b", "c"]
    assert g.is_fallback is False


def test_parse_missing_skills_strips_blanks():
    g = _parse_missing_skills('{"missing_skills": ["Go", "  ", ""]}', job_id="j", provider="ollama", max_skills=6)
    assert g.skills == ["Go"]


def test_parse_missing_skills_fenced_json():
    raw = '```json\n{"missing_skills": ["Rust"]}\n```'
    g = _parse_missing_skills(raw, job_id="j", provider="ollama", max_skills=6)
    assert g.skills == ["Rust"]
    assert g.is_fallback is False


def test_parse_missing_skills_fallback_on_empty():
    g = _parse_missing_skills("", job_id="j", provider="ollama", max_skills=6)
    assert g.skills == []
    assert g.is_fallback is True


def test_parse_missing_skills_fallback_on_non_json():
    g = _parse_missing_skills("not json at all", job_id="j", provider="ollama", max_skills=6)
    assert g.is_fallback is True


def test_parse_missing_skills_fallback_on_missing_key():
    g = _parse_missing_skills('{"other": []}', job_id="j", provider="ollama", max_skills=6)
    assert g.is_fallback is True


def test_parse_missing_skills_fallback_when_not_a_list():
    g = _parse_missing_skills('{"missing_skills": "Kubernetes"}', job_id="j", provider="ollama", max_skills=6)
    assert g.is_fallback is True


import pytest
from unittest.mock import AsyncMock, MagicMock

from src.models import NormalizedPosting


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
        description="Build payments infra. Requires Kubernetes and Kafka.",
        posted_at=None,
        source="greenhouse:stripe",
    )
    base.update(overrides)
    return NormalizedPosting(**base)


def _anthropic_gaps_response(skills):
    block = MagicMock()
    block.type = "tool_use"
    block.name = "record_gaps"
    block.input = {"missing_skills": skills}
    resp = MagicMock()
    resp.content = [block]
    return resp


@pytest.mark.asyncio
async def test_anthropic_gap_happy_path():
    from src.gaps import AnthropicGapAnalyzer
    client = MagicMock()
    client.messages.create = AsyncMock(return_value=_anthropic_gaps_response(["Kubernetes", "Kafka"]))
    analyzer = AnthropicGapAnalyzer(
        client=client, model="claude-haiku-4-5", resume_md="# résumé", timeout_seconds=20, max_skills=6,
    )
    g = await analyzer.analyze(_posting())
    assert g.skills == ["Kubernetes", "Kafka"]
    assert g.is_fallback is False


@pytest.mark.asyncio
async def test_anthropic_gap_empty_is_clean_match():
    from src.gaps import AnthropicGapAnalyzer
    client = MagicMock()
    client.messages.create = AsyncMock(return_value=_anthropic_gaps_response([]))
    analyzer = AnthropicGapAnalyzer(
        client=client, model="claude-haiku-4-5", resume_md="x", timeout_seconds=20, max_skills=6,
    )
    g = await analyzer.analyze(_posting())
    assert g.skills == []
    assert g.is_fallback is False


@pytest.mark.asyncio
async def test_anthropic_gap_fallback_on_network_error():
    import httpx
    from src.gaps import AnthropicGapAnalyzer
    client = MagicMock()
    client.messages.create = AsyncMock(side_effect=httpx.ConnectError("dns"))
    analyzer = AnthropicGapAnalyzer(
        client=client, model="claude-haiku-4-5", resume_md="x", timeout_seconds=20, max_skills=6,
    )
    g = await analyzer.analyze(_posting())
    assert g.skills == []
    assert g.is_fallback is True


@pytest.mark.asyncio
async def test_anthropic_gap_fallback_on_missing_tool_use():
    from src.gaps import AnthropicGapAnalyzer
    text_block = MagicMock()
    text_block.type = "text"
    resp = MagicMock()
    resp.content = [text_block]
    client = MagicMock()
    client.messages.create = AsyncMock(return_value=resp)
    analyzer = AnthropicGapAnalyzer(
        client=client, model="claude-haiku-4-5", resume_md="x", timeout_seconds=20, max_skills=6,
    )
    g = await analyzer.analyze(_posting())
    assert g.is_fallback is True


@pytest.mark.asyncio
async def test_anthropic_gap_caches_resume_and_forces_tool():
    from src.gaps import AnthropicGapAnalyzer
    captured: dict = {}

    async def fake_create(**kwargs):
        captured.update(kwargs)
        return _anthropic_gaps_response(["Kubernetes"])

    client = MagicMock()
    client.messages.create = fake_create
    analyzer = AnthropicGapAnalyzer(
        client=client, model="claude-haiku-4-5", resume_md="MY RESUME TEXT", timeout_seconds=20, max_skills=6,
    )
    await analyzer.analyze(_posting())
    sys_block = captured["system"][-1]
    assert sys_block.get("cache_control") == {"type": "ephemeral"}
    assert "MY RESUME TEXT" in sys_block["text"]
    assert captured["tool_choice"] == {"type": "tool", "name": "record_gaps"}


@pytest.mark.asyncio
async def test_anthropic_gap_truncates_to_max_skills():
    from src.gaps import AnthropicGapAnalyzer
    client = MagicMock()
    client.messages.create = AsyncMock(
        return_value=_anthropic_gaps_response(["a", "b", "c", "d", "e", "f", "g"])
    )
    analyzer = AnthropicGapAnalyzer(
        client=client, model="claude-haiku-4-5", resume_md="x", timeout_seconds=20, max_skills=3,
    )
    g = await analyzer.analyze(_posting())
    assert g.skills == ["a", "b", "c"]


import json


def _gemini_gaps_response(skills):
    resp = MagicMock()
    resp.text = json.dumps({"missing_skills": skills})
    return resp


def _gemini_client_returning(resp):
    client = MagicMock()
    client.aio = MagicMock()
    client.aio.models = MagicMock()
    client.aio.models.generate_content = AsyncMock(return_value=resp)
    return client


@pytest.mark.asyncio
async def test_gemini_gap_happy_path():
    from src.gaps import GeminiGapAnalyzer
    client = _gemini_client_returning(_gemini_gaps_response(["Kubernetes", "Kafka"]))
    analyzer = GeminiGapAnalyzer(
        client=client, model="gemini-2.0-flash", resume_md="# résumé", timeout_seconds=20, max_skills=6,
    )
    g = await analyzer.analyze(_posting())
    assert g.skills == ["Kubernetes", "Kafka"]
    assert g.is_fallback is False


@pytest.mark.asyncio
async def test_gemini_gap_fallback_on_network_error():
    import httpx
    from src.gaps import GeminiGapAnalyzer
    client = MagicMock()
    client.aio = MagicMock()
    client.aio.models = MagicMock()
    client.aio.models.generate_content = AsyncMock(side_effect=httpx.ConnectError("dns"))
    analyzer = GeminiGapAnalyzer(
        client=client, model="gemini-2.0-flash", resume_md="x", timeout_seconds=20, max_skills=6,
    )
    g = await analyzer.analyze(_posting())
    assert g.is_fallback is True


@pytest.mark.asyncio
async def test_gemini_gap_fallback_on_malformed_json():
    from src.gaps import GeminiGapAnalyzer
    resp = MagicMock()
    resp.text = "not json {"
    analyzer = GeminiGapAnalyzer(
        client=_gemini_client_returning(resp), model="gemini-2.0-flash", resume_md="x", timeout_seconds=20, max_skills=6,
    )
    g = await analyzer.analyze(_posting())
    assert g.is_fallback is True
    assert g.error_type == "MalformedResponse"


@pytest.mark.asyncio
async def test_gemini_gap_passes_resume_and_schema():
    from src.gaps import GeminiGapAnalyzer
    captured: dict = {}

    async def fake_generate(**kwargs):
        captured.update(kwargs)
        return _gemini_gaps_response(["Go"])

    client = MagicMock()
    client.aio = MagicMock()
    client.aio.models = MagicMock()
    client.aio.models.generate_content = fake_generate
    analyzer = GeminiGapAnalyzer(
        client=client, model="gemini-2.0-flash", resume_md="MY RESUME TEXT", timeout_seconds=20, max_skills=6,
    )
    await analyzer.analyze(_posting())
    cfg = captured["config"]
    sys_text = getattr(cfg, "system_instruction", None) or (cfg.get("system_instruction") if isinstance(cfg, dict) else None)
    mime = getattr(cfg, "response_mime_type", None) or (cfg.get("response_mime_type") if isinstance(cfg, dict) else None)
    assert "MY RESUME TEXT" in sys_text
    assert mime == "application/json"


@pytest.mark.asyncio
async def test_gemini_gap_fallback_on_timeout():
    import asyncio
    from src.gaps import GeminiGapAnalyzer

    async def slow(**kwargs):
        await asyncio.sleep(10)
        return _gemini_gaps_response(["x"])

    client = MagicMock()
    client.aio = MagicMock()
    client.aio.models = MagicMock()
    client.aio.models.generate_content = slow
    analyzer = GeminiGapAnalyzer(
        client=client, model="gemini-2.0-flash", resume_md="x", timeout_seconds=0, max_skills=6,
    )
    g = await analyzer.analyze(_posting())
    assert g.is_fallback is True


def _ollama_gaps_response(skills=None, content=None):
    if content is None:
        content = json.dumps({"missing_skills": skills or []})
    msg = MagicMock()
    msg.content = content
    resp = MagicMock()
    resp.message = msg
    return resp


@pytest.mark.asyncio
async def test_ollama_gap_happy_path():
    from src.gaps import OllamaGapAnalyzer
    client = MagicMock()
    client.chat = AsyncMock(return_value=_ollama_gaps_response(["Kubernetes", "Kafka"]))
    analyzer = OllamaGapAnalyzer(
        client=client, model="gpt-oss:20b", resume_md="# résumé", timeout_seconds=20, max_skills=6,
    )
    g = await analyzer.analyze(_posting())
    assert g.skills == ["Kubernetes", "Kafka"]
    assert g.is_fallback is False


@pytest.mark.asyncio
async def test_ollama_gap_parses_fenced_json():
    from src.gaps import OllamaGapAnalyzer
    resp = _ollama_gaps_response(content='```json\n{"missing_skills": ["Rust"]}\n```')
    client = MagicMock()
    client.chat = AsyncMock(return_value=resp)
    analyzer = OllamaGapAnalyzer(
        client=client, model="gpt-oss:20b", resume_md="x", timeout_seconds=20, max_skills=6,
    )
    g = await analyzer.analyze(_posting())
    assert g.skills == ["Rust"]


@pytest.mark.asyncio
async def test_ollama_gap_fallback_on_empty_content():
    from src.gaps import OllamaGapAnalyzer
    client = MagicMock()
    client.chat = AsyncMock(return_value=_ollama_gaps_response(content=""))
    analyzer = OllamaGapAnalyzer(
        client=client, model="gpt-oss:20b", resume_md="x", timeout_seconds=20, max_skills=6,
    )
    g = await analyzer.analyze(_posting())
    assert g.is_fallback is True


@pytest.mark.asyncio
async def test_ollama_gap_request_shape():
    from src.gaps import OllamaGapAnalyzer
    captured: dict = {}

    async def fake_chat(**kwargs):
        captured.update(kwargs)
        return _ollama_gaps_response(["Go"])

    client = MagicMock()
    client.chat = fake_chat
    analyzer = OllamaGapAnalyzer(
        client=client, model="gpt-oss:20b", resume_md="MY RESUME TEXT", timeout_seconds=20, max_skills=6,
    )
    await analyzer.analyze(_posting())
    assert captured["think"] is False
    assert captured["options"]["temperature"] == 0
    assert captured["options"]["num_predict"] >= 512
    sys_msg = [m for m in captured["messages"] if m["role"] == "system"][0]["content"]
    assert "MY RESUME TEXT" in sys_msg
    assert "JSON" in sys_msg


@pytest.mark.asyncio
async def test_ollama_gap_fallback_on_non_string_content():
    from src.gaps import OllamaGapAnalyzer
    client = MagicMock()
    client.chat = AsyncMock(return_value=_ollama_gaps_response(content={"missing_skills": ["x"]}))
    analyzer = OllamaGapAnalyzer(
        client=client, model="gpt-oss:20b", resume_md="x", timeout_seconds=20, max_skills=6,
    )
    g = await analyzer.analyze(_posting())
    assert g.is_fallback is True


# --- error_type tests ---

@pytest.mark.asyncio
async def test_gaps_fallback_carries_exception_error_type():
    import httpx
    from src.gaps import AnthropicGapAnalyzer
    client = MagicMock()
    client.messages.create = AsyncMock(side_effect=httpx.ConnectError("dns"))
    analyzer = AnthropicGapAnalyzer(
        client=client, model="claude-haiku-4-5", resume_md="x", timeout_seconds=10, max_skills=8,
    )
    result = await analyzer.analyze(_posting())
    assert result.is_fallback is True
    assert result.error_type == "ConnectError"


@pytest.mark.asyncio
async def test_gaps_malformed_response_error_type():
    """A tool_use block missing missing_skills key -> 'MalformedResponse'."""
    from src.gaps import AnthropicGapAnalyzer
    block = MagicMock()
    block.type = "tool_use"
    block.name = "record_gaps"
    block.input = {}  # no missing_skills key
    resp = MagicMock()
    resp.content = [block]
    client = MagicMock()
    client.messages.create = AsyncMock(return_value=resp)
    analyzer = AnthropicGapAnalyzer(
        client=client, model="claude-haiku-4-5", resume_md="x", timeout_seconds=10, max_skills=8,
    )
    result = await analyzer.analyze(_posting())
    assert result.is_fallback is True
    assert result.error_type == "MalformedResponse"


@pytest.mark.asyncio
async def test_gaps_success_has_no_error_type():
    from src.gaps import AnthropicGapAnalyzer
    client = MagicMock()
    client.messages.create = AsyncMock(return_value=_anthropic_gaps_response(["Kubernetes"]))
    analyzer = AnthropicGapAnalyzer(
        client=client, model="claude-haiku-4-5", resume_md="x", timeout_seconds=10, max_skills=8,
    )
    result = await analyzer.analyze(_posting())
    assert result.is_fallback is False
    assert result.error_type is None


@pytest.mark.asyncio
async def test_anthropic_gap_fallback_on_timeout():
    """The SDK's ``timeout=`` is per attempt and it retries, so the analyzer
    must bound the whole call itself."""
    import asyncio
    from src.gaps import AnthropicGapAnalyzer

    async def slow(**kwargs):
        await asyncio.sleep(30)

    client = MagicMock()
    client.messages.create = slow
    analyzer = AnthropicGapAnalyzer(
        client=client, model="claude-haiku-4-5", resume_md="x", timeout_seconds=0.05, max_skills=6,
    )
    g = await asyncio.wait_for(analyzer.analyze(_posting()), timeout=5)
    assert g.is_fallback is True
