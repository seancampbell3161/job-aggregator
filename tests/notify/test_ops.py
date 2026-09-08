# tests/notify/test_ops.py
import httpx
import pytest

from src.notify.ops import OpsAlert, send_ops_alert


def _alert(recovered=False):
    return OpsAlert(
        condition="zero_yield", title="pipeline zero-yield",
        body="No new postings in 12h.", recovered=recovered,
    )


@pytest.mark.asyncio
async def test_sends_to_both_sinks():
    seen: list[httpx.Request] = []
    def h(request):
        seen.append(request)
        return httpx.Response(200)
    async with httpx.AsyncClient(transport=httpx.MockTransport(h)) as client:
        ok = await send_ops_alert(
            client, _alert(),
            ntfy_topic_url="https://ntfy.test/ops",
            discord_webhook_url="https://discord.test/ops",
        )
    assert ok is True
    assert [str(r.url) for r in seen] == ["https://ntfy.test/ops", "https://discord.test/ops"]
    ntfy = seen[0]
    assert ntfy.headers["Title"] == "pipeline zero-yield"
    assert ntfy.headers["Priority"] == "high"
    assert ntfy.headers["Tags"] == "warning"
    assert b"No new postings" in ntfy.content


@pytest.mark.asyncio
async def test_recovered_alert_uses_calm_priority():
    seen: list[httpx.Request] = []
    def h(request):
        seen.append(request)
        return httpx.Response(200)
    async with httpx.AsyncClient(transport=httpx.MockTransport(h)) as client:
        await send_ops_alert(client, _alert(recovered=True), ntfy_topic_url="https://ntfy.test/ops")
    assert seen[0].headers["Priority"] == "default"
    assert seen[0].headers["Tags"] == "white_check_mark"


@pytest.mark.asyncio
async def test_one_sink_failing_still_returns_true():
    def h(request):
        if "ntfy" in str(request.url):
            return httpx.Response(500)
        return httpx.Response(200)
    async with httpx.AsyncClient(transport=httpx.MockTransport(h)) as client:
        ok = await send_ops_alert(
            client, _alert(),
            ntfy_topic_url="https://ntfy.test/ops",
            discord_webhook_url="https://discord.test/ops",
        )
    assert ok is True


@pytest.mark.asyncio
async def test_no_sinks_configured_returns_false():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200))) as client:
        assert await send_ops_alert(client, _alert()) is False


@pytest.mark.asyncio
async def test_non_ascii_title_is_latin1_safe():
    seen: list[httpx.Request] = []
    def h(request):
        seen.append(request)
        return httpx.Response(200)
    alert = OpsAlert(condition="x", title="pipeline — stopped", body="b")
    async with httpx.AsyncClient(transport=httpx.MockTransport(h)) as client:
        await send_ops_alert(client, alert, ntfy_topic_url="https://ntfy.test/ops")
    seen[0].headers["Title"].encode("latin-1")  # must not raise
