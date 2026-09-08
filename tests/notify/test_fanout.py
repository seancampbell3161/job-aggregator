import httpx
import pytest

from src.notify.base import NotificationPayload, fanout


PAYLOAD = NotificationPayload(
    title="t", company="c", role="r", location="l", comp=None,
    stack_matched=[], posted="now", apply_url="u", source="s", seniority=None,
)


class _OkSink:
    name = "ok"
    def __init__(self): self.calls = 0
    async def send(self, client, payload): self.calls += 1


class _BadSink:
    name = "bad"
    async def send(self, client, payload):
        raise RuntimeError("boom")


@pytest.mark.asyncio
async def test_fanout_returns_per_sink_result():
    ok = _OkSink()
    bad = _BadSink()
    async with httpx.AsyncClient() as client:
        results = await fanout(client, PAYLOAD, [ok, bad])
    assert results["ok"] is True
    assert isinstance(results["bad"], Exception)
    assert ok.calls == 1


@pytest.mark.asyncio
async def test_fanout_at_least_one_success():
    ok = _OkSink()
    bad = _BadSink()
    async with httpx.AsyncClient() as client:
        results = await fanout(client, PAYLOAD, [ok, bad])
    assert any(v is True for v in results.values())
