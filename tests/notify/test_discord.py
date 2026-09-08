import json

import httpx
import pytest
import respx

from src.notify.base import NotificationPayload
from src.notify.discord import DiscordSink


PAYLOAD = NotificationPayload(
    title="Senior Backend Engineer @ Stripe",
    company="Stripe",
    role="Senior Backend Engineer",
    location="Remote, US",
    comp="$180k-$240k",
    stack_matched=["python", "go"],
    posted="2 min ago",
    apply_url="https://stripe.com/jobs/1",
    source="greenhouse:stripe",
    seniority="senior",
    tags=["senior", "remote"],
)


@pytest.mark.asyncio
async def test_discord_send_posts_embed():
    sink = DiscordSink(webhook_url="https://discord.test/webhook")
    async with httpx.AsyncClient() as client:
        with respx.mock:
            route = respx.post("https://discord.test/webhook").respond(204)
            await sink.send(client, PAYLOAD)
        body = json.loads(route.calls.last.request.content.decode())
        embed = body["embeds"][0]
        assert embed["title"] == "Senior Backend Engineer @ Stripe"
        assert embed["url"] == "https://stripe.com/jobs/1"
        names = {f["name"] for f in embed["fields"]}
        assert {"Company", "Location", "Comp", "Stack", "Posted", "Source"}.issubset(names)
        assert "color" in embed


@pytest.mark.asyncio
async def test_discord_omits_comp_field_when_missing():
    sink = DiscordSink(webhook_url="https://discord.test/webhook")
    payload = NotificationPayload(**{**PAYLOAD.__dict__, "comp": None})
    async with httpx.AsyncClient() as client:
        with respx.mock:
            route = respx.post("https://discord.test/webhook").respond(204)
            await sink.send(client, payload)
        body = json.loads(route.calls.last.request.content.decode())
        names = {f["name"] for f in body["embeds"][0]["fields"]}
        assert "Comp" not in names


@pytest.mark.asyncio
async def test_discord_raises_on_5xx():
    sink = DiscordSink(webhook_url="https://discord.test/webhook")
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.post("https://discord.test/webhook").respond(500)
            with pytest.raises(httpx.HTTPStatusError):
                await sink.send(client, PAYLOAD)


@pytest.mark.asyncio
async def test_discord_retries_after_429(monkeypatch):
    sink = DiscordSink(webhook_url="https://discord.test/webhook")
    sleeps: list[float] = []

    async def _fake_sleep(s: float) -> None:
        sleeps.append(s)

    monkeypatch.setattr("src.notify.discord.asyncio.sleep", _fake_sleep)

    async with httpx.AsyncClient() as client:
        with respx.mock:
            route = respx.post("https://discord.test/webhook").mock(
                side_effect=[
                    httpx.Response(
                        429,
                        json={
                            "message": "Service resource is being rate limited.",
                            "retry_after": 1.5,
                            "global": False,
                            "code": 40062,
                        },
                    ),
                    httpx.Response(204),
                ]
            )
            await sink.send(client, PAYLOAD)
        assert route.call_count == 2
        assert sleeps == [1.5]


@pytest.mark.asyncio
async def test_discord_raises_on_persistent_429(monkeypatch):
    sink = DiscordSink(webhook_url="https://discord.test/webhook")

    async def _fake_sleep(_s: float) -> None:
        return None

    monkeypatch.setattr("src.notify.discord.asyncio.sleep", _fake_sleep)

    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.post("https://discord.test/webhook").mock(
                side_effect=[
                    httpx.Response(429, json={"retry_after": 0.1}),
                    httpx.Response(429, json={"retry_after": 0.1}),
                ]
            )
            with pytest.raises(httpx.HTTPStatusError):
                await sink.send(client, PAYLOAD)


@pytest.mark.asyncio
async def test_discord_retry_after_caps_at_max(monkeypatch):
    sink = DiscordSink(webhook_url="https://discord.test/webhook")
    sleeps: list[float] = []

    async def _fake_sleep(s: float) -> None:
        sleeps.append(s)

    monkeypatch.setattr("src.notify.discord.asyncio.sleep", _fake_sleep)

    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.post("https://discord.test/webhook").mock(
                side_effect=[
                    httpx.Response(429, json={"retry_after": 999}),
                    httpx.Response(204),
                ]
            )
            await sink.send(client, PAYLOAD)
        assert sleeps == [10.0]


@pytest.mark.asyncio
async def test_discord_retry_after_falls_back_to_header(monkeypatch):
    sink = DiscordSink(webhook_url="https://discord.test/webhook")
    sleeps: list[float] = []

    async def _fake_sleep(s: float) -> None:
        sleeps.append(s)

    monkeypatch.setattr("src.notify.discord.asyncio.sleep", _fake_sleep)

    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.post("https://discord.test/webhook").mock(
                side_effect=[
                    httpx.Response(429, headers={"Retry-After": "2"}, text="not json"),
                    httpx.Response(204),
                ]
            )
            await sink.send(client, PAYLOAD)
        assert sleeps == [2.0]


@pytest.mark.asyncio
async def test_discord_sink_includes_score_in_title():
    captured: dict = {}

    class _StubClient:
        async def post(self, url, *, json, timeout):
            captured["json"] = json

            class _Resp:
                status_code = 200
                text = ''
                def raise_for_status(self): pass
            return _Resp()

    sink = DiscordSink(webhook_url="https://discord.test/x")
    payload = NotificationPayload(
        title="Senior Backend @ Stripe", company="Stripe", role="Senior Backend",
        location="Remote", comp=None, stack_matched=[], posted="now",
        apply_url="https://x", source="src", seniority="senior", tags=[],
        relevance_score=8, relevance_rationale="Solid fit", relevance_priority="default",
    )
    await sink.send(_StubClient(), payload)

    embed = captured["json"]["embeds"][0]
    assert embed["title"].startswith("[8/10] ")
    assert embed["description"] == "Solid fit"


@pytest.mark.asyncio
async def test_discord_renders_stretch_areas_when_gaps_present():
    sink = DiscordSink(webhook_url="https://discord.test/webhook")
    payload = NotificationPayload(**{**PAYLOAD.__dict__, "gaps": ["Kubernetes", "Kafka"]})
    async with httpx.AsyncClient() as client:
        with respx.mock:
            route = respx.post("https://discord.test/webhook").respond(204)
            await sink.send(client, payload)
        body = json.loads(route.calls.last.request.content.decode())
        fields = {f["name"]: f["value"] for f in body["embeds"][0]["fields"]}
        assert fields["Stretch areas"] == "Kubernetes, Kafka"


@pytest.mark.asyncio
async def test_discord_omits_stretch_areas_when_no_gaps():
    sink = DiscordSink(webhook_url="https://discord.test/webhook")
    async with httpx.AsyncClient() as client:
        with respx.mock:
            route = respx.post("https://discord.test/webhook").respond(204)
            await sink.send(client, PAYLOAD)   # PAYLOAD has no gaps → defaults to []
        body = json.loads(route.calls.last.request.content.decode())
        names = {f["name"] for f in body["embeds"][0]["fields"]}
        assert "Stretch areas" not in names


@pytest.mark.asyncio
async def test_discord_sink_no_score_prefix_when_relevance_disabled():
    captured: dict = {}

    class _StubClient:
        async def post(self, url, *, json, timeout):
            captured["json"] = json

            class _Resp:
                status_code = 200
                text = ''
                def raise_for_status(self): pass
            return _Resp()

    sink = DiscordSink(webhook_url="https://discord.test/x")
    payload = NotificationPayload(
        title="Senior Backend @ Stripe", company="Stripe", role="Senior Backend",
        location="Remote", comp=None, stack_matched=[], posted="now",
        apply_url="https://x", source="src", seniority="senior", tags=[],
        # No relevance fields set
    )
    await sink.send(_StubClient(), payload)

    embed = captured["json"]["embeds"][0]
    assert not embed["title"].startswith("[")
    assert "description" not in embed


@pytest.mark.asyncio
async def test_discord_adds_tailor_field_when_url():
    sink = DiscordSink(webhook_url="https://discord.test/webhook")
    p = NotificationPayload(**{**PAYLOAD.__dict__, "tailor_url": "https://ep.example?job_id=j&t=tok"})
    async with httpx.AsyncClient() as client:
        with respx.mock:
            route = respx.post("https://discord.test/webhook").respond(204)
            await sink.send(client, p)
        fields = json.loads(route.calls.last.request.content.decode())["embeds"][0]["fields"]
        tailor = next(f for f in fields if "Tailor" in f["name"])
        assert "https://ep.example?job_id=j&t=tok" in tailor["value"]


@pytest.mark.asyncio
async def test_discord_no_tailor_field_without_url():
    sink = DiscordSink(webhook_url="https://discord.test/webhook")
    async with httpx.AsyncClient() as client:
        with respx.mock:
            route = respx.post("https://discord.test/webhook").respond(204)
            await sink.send(client, PAYLOAD)   # tailor_url None
        names = {f["name"] for f in json.loads(route.calls.last.request.content.decode())["embeds"][0]["fields"]}
        assert not any("Tailor" in n for n in names)


@pytest.mark.asyncio
async def test_discord_adzuna_source_field_links_attribution():
    payload = NotificationPayload(**{**PAYLOAD.__dict__, "source": "adzuna"})
    sink = DiscordSink(webhook_url="https://discord.test/webhook")
    async with httpx.AsyncClient() as client:
        with respx.mock:
            route = respx.post("https://discord.test/webhook").respond(204)
            await sink.send(client, payload)
    body = json.loads(route.calls.last.request.content.decode())
    source_field = next(f for f in body["embeds"][0]["fields"] if f["name"] == "Source")
    assert source_field["value"] == "[Jobs by Adzuna](https://www.adzuna.com)"


@pytest.mark.asyncio
async def test_discord_source_field_stays_raw_for_other_sources():
    sink = DiscordSink(webhook_url="https://discord.test/webhook")
    async with httpx.AsyncClient() as client:
        with respx.mock:
            route = respx.post("https://discord.test/webhook").respond(204)
            await sink.send(client, PAYLOAD)
    body = json.loads(route.calls.last.request.content.decode())
    source_field = next(f for f in body["embeds"][0]["fields"] if f["name"] == "Source")
    assert source_field["value"] == "greenhouse:stripe"
    assert "Adzuna" not in source_field["value"]
