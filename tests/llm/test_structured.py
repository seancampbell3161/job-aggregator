"""One JSON Schema, three provider dialects — same parsed result."""
import asyncio
import json

import pytest

from src.llm.providers import LlmBinding
from src.llm.structured import complete_json, complete_text

SCHEMA = {
    "type": "object",
    "properties": {"colour": {"type": "string"}, "count": {"type": "integer"}},
    "required": ["colour", "count"],
}
EXPECTED = {"colour": "blue", "count": 3}


class _FakeAnthropic:
    def __init__(self, payload, *, stop_reason="tool_use"):
        self.payload, self.stop_reason, self.kwargs = payload, stop_reason, None
        self.messages = self

    async def create(self, **kwargs):
        self.kwargs = kwargs
        block = type("B", (), {"type": "tool_use", "input": self.payload})()
        return type("R", (), {"content": [block], "stop_reason": self.stop_reason})()


class _FakeGemini:
    def __init__(self, text):
        self.text, self.kwargs = text, None
        self.aio = type("A", (), {"models": self})()

    async def generate_content(self, **kwargs):
        self.kwargs = kwargs
        return type("R", (), {"text": self.text})()


class _FakeOllama:
    def __init__(self, text):
        self.text, self.kwargs = text, None

    async def chat(self, **kwargs):
        self.kwargs = kwargs
        return {"message": {"content": self.text}}


def _binding(provider, client):
    return LlmBinding(provider=provider, model="m", client=client, timeout_seconds=5)


@pytest.mark.asyncio
async def test_anthropic_returns_the_tool_input():
    client = _FakeAnthropic(EXPECTED)
    got = await complete_json(_binding("anthropic", client), system="s", user="u", schema=SCHEMA)
    assert got == EXPECTED


@pytest.mark.asyncio
async def test_anthropic_forces_the_tool():
    client = _FakeAnthropic(EXPECTED)
    await complete_json(_binding("anthropic", client), system="s", user="u", schema=SCHEMA)
    assert client.kwargs["tool_choice"] == {"type": "tool", "name": "respond"}
    assert client.kwargs["tools"][0]["input_schema"] == SCHEMA
    assert client.kwargs["system"] == "s"


@pytest.mark.asyncio
async def test_gemini_parses_its_json_text():
    client = _FakeGemini(json.dumps(EXPECTED))
    got = await complete_json(_binding("gemini", client), system="s", user="u", schema=SCHEMA)
    assert got == EXPECTED


@pytest.mark.asyncio
async def test_gemini_asks_for_json_against_the_same_schema():
    client = _FakeGemini(json.dumps(EXPECTED))
    await complete_json(_binding("gemini", client), system="s", user="u", schema=SCHEMA)
    cfg = client.kwargs["config"]
    as_dict = cfg if isinstance(cfg, dict) else vars(cfg)
    assert as_dict["response_mime_type"] == "application/json"
    assert as_dict["response_json_schema"] == SCHEMA


@pytest.mark.asyncio
async def test_ollama_parses_fenced_json():
    """Ollama gets no schema support, only prompt instructions — so the parser
    has to survive a model that wraps its answer in a code fence."""
    client = _FakeOllama("Sure!\n```json\n" + json.dumps(EXPECTED) + "\n```\n")
    got = await complete_json(_binding("ollama", client), system="s", user="u", schema=SCHEMA)
    assert got == EXPECTED


@pytest.mark.asyncio
async def test_ollama_puts_the_schema_in_the_prompt():
    client = _FakeOllama(json.dumps(EXPECTED))
    await complete_json(_binding("ollama", client), system="s", user="u", schema=SCHEMA)
    system_text = client.kwargs["messages"][0]["content"]
    assert "colour" in system_text and "JSON" in system_text


@pytest.mark.asyncio
async def test_all_three_providers_agree():
    """The parity property this module exists for."""
    results = [
        await complete_json(_binding("anthropic", _FakeAnthropic(EXPECTED)),
                            system="s", user="u", schema=SCHEMA),
        await complete_json(_binding("gemini", _FakeGemini(json.dumps(EXPECTED))),
                            system="s", user="u", schema=SCHEMA),
        await complete_json(_binding("ollama", _FakeOllama(json.dumps(EXPECTED))),
                            system="s", user="u", schema=SCHEMA),
    ]
    assert results == [EXPECTED, EXPECTED, EXPECTED]


@pytest.mark.asyncio
async def test_unparseable_text_is_none_not_an_exception():
    client = _FakeOllama("I'm afraid I can't do that.")
    assert await complete_json(_binding("ollama", client), system="s", user="u", schema=SCHEMA) is None


@pytest.mark.asyncio
async def test_anthropic_without_a_tool_block_is_none():
    """max_tokens exhaustion returns text, not a tool_use block."""
    client = _FakeAnthropic(EXPECTED, stop_reason="max_tokens")
    client.messages = client
    async def create(**kwargs):
        return type("R", (), {"content": [type("B", (), {"type": "text", "text": "..."})()],
                              "stop_reason": "max_tokens"})()
    client.create = create
    assert await complete_json(_binding("anthropic", client), system="s", user="u", schema=SCHEMA) is None


@pytest.mark.asyncio
async def test_provider_errors_propagate():
    """complete_json does NOT fail open: résumé drafting must be able to tell
    a failed call from an empty answer."""
    class _Boom:
        def __init__(self): self.messages = self
        async def create(self, **kwargs): raise RuntimeError("down")

    with pytest.raises(RuntimeError):
        await complete_json(_binding("anthropic", _Boom()), system="s", user="u", schema=SCHEMA)


@pytest.mark.asyncio
async def test_anthropic_gets_the_output_budget():
    client = _FakeAnthropic(EXPECTED)
    await complete_json(_binding("anthropic", client), system="s", user="u",
                        schema=SCHEMA, max_output_tokens=16384)
    assert client.kwargs["max_tokens"] == 16384


@pytest.mark.asyncio
async def test_gemini_gets_the_output_budget():
    client = _FakeGemini(json.dumps(EXPECTED))
    await complete_json(_binding("gemini", client), system="s", user="u",
                        schema=SCHEMA, max_output_tokens=16384)
    cfg = client.kwargs["config"]
    as_dict = cfg if isinstance(cfg, dict) else vars(cfg)
    assert as_dict["max_output_tokens"] == 16384


@pytest.mark.asyncio
async def test_ollama_gets_the_budget_and_runs_deterministically():
    """The two call sites being ported onto this seam both set temperature 0
    and think=False; gpt-oss otherwise spends output budget reasoning."""
    client = _FakeOllama(json.dumps(EXPECTED))
    await complete_json(_binding("ollama", client), system="s", user="u",
                        schema=SCHEMA, max_output_tokens=16384)
    assert client.kwargs["options"]["num_predict"] == 16384
    assert client.kwargs["options"]["temperature"] == 0
    assert client.kwargs["think"] is False


@pytest.mark.asyncio
async def test_the_default_budget_is_what_existing_callers_already_had():
    """resume_draft calls complete_json with no budget. It must keep 4096 —
    a smaller default would quietly shorten a shipped feature's output."""
    client = _FakeAnthropic(EXPECTED)
    await complete_json(_binding("anthropic", client), system="s", user="u", schema=SCHEMA)
    assert client.kwargs["max_tokens"] == 4096


TEXT = "<html><body>{{ doc.name }}</body></html>"


@pytest.mark.asyncio
async def test_complete_text_on_all_three_providers():
    class _FakeAnthropicText:
        def __init__(self):
            self.kwargs = None
            self.messages = self

        async def create(self, **kwargs):
            self.kwargs = kwargs
            block = type("B", (), {"type": "text", "text": TEXT})()
            return type("R", (), {"content": [block], "stop_reason": "end_turn"})()

    anthropic = _FakeAnthropicText()
    gemini = _FakeGemini(TEXT)
    ollama = _FakeOllama(TEXT)
    results = [
        await complete_text(_binding("anthropic", anthropic), system="s", user="u",
                            max_output_tokens=16384),
        await complete_text(_binding("gemini", gemini), system="s", user="u",
                            max_output_tokens=16384),
        await complete_text(_binding("ollama", ollama), system="s", user="u",
                            max_output_tokens=16384),
    ]
    assert results == [TEXT, TEXT, TEXT]
    assert anthropic.kwargs["max_tokens"] == 16384
    assert ollama.kwargs["options"]["num_predict"] == 16384


@pytest.mark.asyncio
async def test_complete_text_asks_gemini_for_text_not_json():
    """A JSON mime type would make the model wrap an HTML template in a JSON
    string — valid output that is useless to the caller."""
    client = _FakeGemini(TEXT)
    await complete_text(_binding("gemini", client), system="s", user="u",
                        max_output_tokens=16384)
    cfg = client.kwargs["config"]
    as_dict = cfg if isinstance(cfg, dict) else vars(cfg)
    assert as_dict.get("response_mime_type") is None
    assert as_dict.get("response_json_schema") is None
    assert as_dict["max_output_tokens"] == 16384


class _FakeAnthropicEmptyText:
    def __init__(self):
        self.messages = self

    async def create(self, **kwargs):
        block = type("B", (), {"type": "text", "text": ""})()
        return type("R", (), {"content": [block], "stop_reason": "end_turn"})()


@pytest.mark.asyncio
@pytest.mark.parametrize("provider, client", [
    ("anthropic", _FakeAnthropicEmptyText()),
    ("gemini", _FakeGemini("")),
    ("ollama", _FakeOllama("")),
], ids=["anthropic", "gemini", "ollama"])
async def test_complete_text_returns_none_for_empty_output(provider, client):
    """Each provider has its own empty→None coercion, so each needs its own
    check: "answered with nothing" must read the same whoever answered."""
    assert await complete_text(_binding(provider, client), system="s",
                               user="u", max_output_tokens=64) is None


@pytest.mark.asyncio
async def test_complete_text_does_not_fail_open():
    class _Boom:
        def __init__(self): self.messages = self
        async def create(self, **kwargs): raise RuntimeError("down")

    with pytest.raises(RuntimeError):
        await complete_text(_binding("anthropic", _Boom()), system="s", user="u",
                            max_output_tokens=64)


class _HangingAnthropic:
    """An SDK that honours its per-attempt ``timeout`` but retries: the total
    wall time is a multiple of it. Modelled as one call that ignores the
    kwarg, which is what the caller observes."""
    def __init__(self):
        self.messages = self

    async def create(self, **kwargs):
        await asyncio.sleep(30)


@pytest.mark.asyncio
@pytest.mark.parametrize("call", ["json", "text"])
async def test_anthropic_is_bounded_by_the_binding_timeout(call):
    """The SDK's ``timeout=`` is per attempt; with its default two retries a
    call could run ~3x ``timeout_seconds``. The binding's timeout is the
    caller's whole budget, so it must bound the call end to end."""
    binding = LlmBinding(provider="anthropic", model="m",
                         client=_HangingAnthropic(), timeout_seconds=0.05)
    with pytest.raises(asyncio.TimeoutError):
        if call == "json":
            await complete_json(binding, system="s", user="u", schema=SCHEMA)
        else:
            await complete_text(binding, system="s", user="u", max_output_tokens=64)
