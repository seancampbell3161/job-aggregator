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
async def test_llm_probe_reports_a_disabled_scorer():
    cfg = AppConfig.model_validate({"relevance": {"enabled": False}})
    r = await probe_llm(cfg, "# profile")
    assert not r.ok
    assert "not configured" in r.detail.lower()


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
