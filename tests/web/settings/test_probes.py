"""Test buttons. They must use the real sinks, so these tests stub transport,
not the sink classes."""
import httpx
import pytest

from src.config import AppConfig
from src.web.settings.probes import probe_discord, probe_llm, probe_ntfy


def _transport(handler):
    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_ntfy_probe_posts_to_the_topic(monkeypatch):
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200)

    monkeypatch.setattr("src.web.settings.probes._transport", lambda: _transport(handler))
    r = await probe_ntfy("https://ntfy.sh/my-topic")
    assert r.ok
    assert seen["url"] == "https://ntfy.sh/my-topic"
    assert "check your phone" in r.detail.lower()


@pytest.mark.asyncio
async def test_ntfy_probe_reports_an_http_error(monkeypatch):
    monkeypatch.setattr("src.web.settings.probes._transport",
                        lambda: _transport(lambda req: httpx.Response(404, text="nope")))
    r = await probe_ntfy("https://ntfy.sh/my-topic")
    assert not r.ok and "404" in r.detail


@pytest.mark.asyncio
async def test_discord_probe_posts_to_the_webhook(monkeypatch):
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(204)

    monkeypatch.setattr("src.web.settings.probes._transport", lambda: _transport(handler))
    r = await probe_discord("https://discord.com/api/webhooks/1/x")
    assert r.ok and seen["url"].endswith("/webhooks/1/x")


@pytest.mark.asyncio
async def test_a_non_http_scheme_is_refused():
    r = await probe_ntfy("file:///etc/passwd")
    assert not r.ok and "http" in r.detail.lower()


@pytest.mark.asyncio
async def test_an_uppercase_and_whitespace_padded_scheme_is_still_refused():
    # urlparse lowercases the scheme itself, but a raw string like this is a
    # plausible way someone pastes a URL into a form field, and the check must
    # not depend on urlparse's normalization alone doing the work silently.
    r = await probe_discord("  JAVASCRIPT://ntfy.sh/x  ")
    assert not r.ok and "http" in r.detail.lower()


@pytest.mark.asyncio
async def test_an_empty_url_is_refused():
    r = await probe_discord("")
    assert not r.ok and "no url" in r.detail.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_url", ["https://[", "http://[::1", "https://[abc]x:1]"])
async def test_a_malformed_bracket_url_is_refused_not_raised(bad_url):
    # urlparse raises ValueError("Invalid IPv6 URL") on an unbalanced bracket
    # instead of returning a normal ParseResult — the probe must catch that
    # and report it, never let it 500 the page.
    r = await probe_ntfy(bad_url)
    assert not r.ok
    assert "does not look like a url" in r.detail.lower()


@pytest.mark.asyncio
async def test_a_control_character_in_the_url_is_refused():
    r = await probe_discord("https://discord.com/api/webhooks/1/x\x00y")
    assert not r.ok
    assert "does not look like a url" in r.detail.lower()


@pytest.mark.asyncio
async def test_llm_probe_says_so_when_scoring_is_switched_off():
    """One reason per message. The old text listed all three possible causes
    at once, so it never told the user which one was theirs."""
    cfg = AppConfig.model_validate({"relevance": {"enabled": False}})
    r = await probe_llm(cfg, "# profile")
    assert not r.ok
    assert "enabled" in r.detail.lower()
    assert "api key" not in r.detail.lower()


@pytest.mark.asyncio
async def test_llm_probe_names_the_secret_the_provider_is_missing():
    cfg = AppConfig.model_validate(
        {"relevance": {"enabled": True, "provider": "anthropic"}})
    r = await probe_llm(cfg, "# profile")
    assert not r.ok
    assert "anthropic_api_key" in r.detail


@pytest.mark.asyncio
async def test_llm_probe_does_not_ask_local_ollama_for_an_api_key(monkeypatch):
    """A local Ollama needs no key, so the probe must get as far as actually
    calling it rather than refusing up front — the old message demanded a key
    for every provider."""
    from src.relevance import Score

    class FakeScorer:
        async def score(self, posting):
            return Score(value=6, rationale="ok", is_fallback=False)

    monkeypatch.setattr("src.web.settings.probes._build_scorer",
                        lambda cfg, profile: FakeScorer())
    cfg = AppConfig.model_validate({"relevance": {
        "enabled": True, "provider": "ollama",
        "ollama_host": "http://127.0.0.1:11434"}})
    r = await probe_llm(cfg, None)
    assert r.ok, r.detail


@pytest.mark.asyncio
async def test_llm_probe_scores_a_sample_posting(monkeypatch):
    from src.relevance import Score

    class FakeScorer:
        async def score(self, posting):
            return Score(value=8, rationale="Strong fit", is_fallback=False)

    monkeypatch.setattr("src.web.settings.probes._build_scorer", lambda cfg, profile: FakeScorer())
    cfg = AppConfig.model_validate({"relevance": {"enabled": True}})
    r = await probe_llm(cfg, "# profile")
    assert r.ok and "8" in r.detail


@pytest.mark.asyncio
async def test_llm_probe_works_before_a_profile_document_exists(monkeypatch):
    """The wizard's LLM step comes two steps before the profile document is
    written, so at first-run setup there is nothing to grade against. The probe
    is answering "can I reach this provider?", not "is scoring ready?" — so it
    falls back to a sample profile rather than refusing, which would make the
    button impossible to pass on the one screen that most needs it."""
    from src.relevance import Score

    seen = {}

    class FakeScorer:
        async def score(self, posting):
            return Score(value=8, rationale="Strong fit", is_fallback=False)

    def fake_build(cfg, profile):
        seen["profile"] = profile
        return FakeScorer()

    monkeypatch.setattr("src.web.settings.probes._build_scorer", fake_build)
    cfg = AppConfig.model_validate({"relevance": {"enabled": True}})
    r = await probe_llm(cfg, None)
    assert r.ok, r.detail
    assert seen["profile"], "the probe must supply a stand-in profile, not None"


@pytest.mark.asyncio
async def test_llm_probe_prefers_the_real_profile_when_there_is_one(monkeypatch):
    """The stand-in is only for the not-yet-written case; once the user has a
    profile the probe must exercise the real thing."""
    from src.relevance import Score

    seen = {}

    class FakeScorer:
        async def score(self, posting):
            return Score(value=8, rationale="Strong fit", is_fallback=False)

    def fake_build(cfg, profile):
        seen["profile"] = profile
        return FakeScorer()

    monkeypatch.setattr("src.web.settings.probes._build_scorer", fake_build)
    cfg = AppConfig.model_validate({"relevance": {"enabled": True}})
    await probe_llm(cfg, "# my real profile")
    assert seen["profile"] == "# my real profile"


@pytest.mark.asyncio
async def test_llm_probe_reports_a_provider_failure(monkeypatch):
    class Boom:
        async def score(self, posting):
            raise RuntimeError("401 unauthorized")

    monkeypatch.setattr("src.web.settings.probes._build_scorer", lambda cfg, profile: Boom())
    cfg = AppConfig.model_validate({"relevance": {"enabled": True}})
    r = await probe_llm(cfg, "# profile")
    assert not r.ok and "401" in r.detail


@pytest.mark.asyncio
async def test_llm_probe_reports_a_fail_open_score_as_a_failure(monkeypatch):
    # The three real scorer implementations never raise out of `score()` — on
    # a provider error they fail open and return a sentinel Score with
    # is_fallback=True (see src/relevance.py). A probe that only looks for a
    # raised exception would call this a success.
    from src.relevance import Score

    class FailsOpen:
        async def score(self, posting):
            return Score(value=None, rationale="(LLM unavailable)",
                         is_fallback=True, error_type="HTTPStatusError")

    monkeypatch.setattr("src.web.settings.probes._build_scorer", lambda cfg, profile: FailsOpen())
    cfg = AppConfig.model_validate({"relevance": {"enabled": True}})
    r = await probe_llm(cfg, "# profile")
    assert not r.ok
    assert "HTTPStatusError" in r.detail


@pytest.mark.asyncio
async def test_llm_probe_reports_a_malformed_provider_config_instead_of_raising():
    # _build_scorer (src.handler._build_relevance_scorer) eagerly constructs a
    # provider client — for Ollama that means the SDK parses ollama_host into
    # an httpx.Client at __init__ time, before any scoring happens. ollama_host
    # is free-form user-entered config (the Task 9 LLM settings page lets you
    # type into it and press Test), so a bad bracket/port is ordinary user
    # error, not a hypothetical. This must come back as a ProbeResult, never
    # raise into the request.
    cfg = AppConfig.model_validate({
        "relevance": {"enabled": True, "provider": "ollama", "ollama_host": "http://[::1"},
    })
    r = await probe_llm(cfg, "# profile")
    assert not r.ok
    assert "invalid" in r.detail.lower()
