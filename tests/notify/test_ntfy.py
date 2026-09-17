from datetime import datetime, time
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx
from freezegun import freeze_time

from src.config import QuietHoursConfig
from src.notify.base import NotificationPayload
from src.notify.ntfy import NtfySink, _is_quiet


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
    tags=["senior", "remote", "python", "go"],
)

QH = QuietHoursConfig(timezone=ZoneInfo("America/Los_Angeles"), start=time(23, 0), end=time(7, 0))


@freeze_time("2026-04-30 18:00:00", tz_offset=0)  # 11:00 PT — daytime
@pytest.mark.asyncio
async def test_ntfy_send_high_priority_outside_quiet():
    sink = NtfySink(topic_url="https://ntfy.sh/test", quiet_hours=QH)
    async with httpx.AsyncClient() as client:
        with respx.mock:
            route = respx.post("https://ntfy.sh/test").respond(200)
            await sink.send(client, PAYLOAD)
        req = route.calls.last.request
        assert req.headers["Priority"] == "default"
        assert "Senior Backend Engineer @ Stripe" in req.headers["Title"]
        assert "Remote, US" in req.headers["Title"]
        assert "remote" in req.headers["Tags"]
        assert req.headers["Click"] == "https://stripe.com/jobs/1"


@freeze_time("2026-04-30 09:00:00", tz_offset=0)  # 02:00 PT — quiet
@pytest.mark.asyncio
async def test_ntfy_send_low_priority_during_quiet():
    sink = NtfySink(topic_url="https://ntfy.sh/test", quiet_hours=QH)
    async with httpx.AsyncClient() as client:
        with respx.mock:
            route = respx.post("https://ntfy.sh/test").respond(200)
            await sink.send(client, PAYLOAD)
        assert route.calls.last.request.headers["Priority"] == "low"


def test_is_quiet_wraparound_window():
    # Window 23:00 -> 07:00, timezone UTC for simplicity
    qh = QuietHoursConfig(timezone=ZoneInfo("UTC"), start=time(23, 0), end=time(7, 0))
    assert _is_quiet(datetime(2026, 1, 1, 23, 30, tzinfo=ZoneInfo("UTC")), qh) is True
    assert _is_quiet(datetime(2026, 1, 1, 3, 0, tzinfo=ZoneInfo("UTC")), qh) is True
    assert _is_quiet(datetime(2026, 1, 1, 12, 0, tzinfo=ZoneInfo("UTC")), qh) is False
    assert _is_quiet(datetime(2026, 1, 1, 7, 0, tzinfo=ZoneInfo("UTC")), qh) is False  # boundary exclusive


@pytest.mark.asyncio
async def test_ntfy_raises_on_5xx():
    sink = NtfySink(topic_url="https://ntfy.sh/test", quiet_hours=QH)
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.post("https://ntfy.sh/test").respond(500)
            with pytest.raises(httpx.HTTPStatusError):
                await sink.send(client, PAYLOAD)


@pytest.mark.asyncio
async def test_ntfy_sink_sets_max_priority_on_high_score(monkeypatch):
    """A payload with relevance_priority='max' is sent with Priority: max."""
    from src.notify.ntfy import NtfySink

    captured: dict = {}

    class _StubClient:
        async def post(self, url, *, content, headers, timeout):
            captured["headers"] = headers

            class _Resp:
                def raise_for_status(self): pass
            return _Resp()

    qh = QuietHoursConfig(timezone=ZoneInfo("UTC"), start=time(23, 0), end=time(7, 0))
    # Force "outside quiet hours": pin now() to noon UTC.
    monkeypatch.setattr(
        "src.notify.ntfy.datetime",
        type("D", (), {
            "now": staticmethod(lambda tz=None: __import__("datetime").datetime(2026, 5, 1, 12, 0, tzinfo=tz)),
        })(),
    )
    sink = NtfySink(topic_url="https://ntfy.test/x", quiet_hours=qh)
    payload = NotificationPayload(
        title="X @ Y", company="Y", role="X", location="Remote", comp=None,
        stack_matched=[], posted="now", apply_url="https://x", source="src",
        seniority="senior", tags=[],
        relevance_score=9, relevance_rationale="Strong", relevance_priority="max",
    )
    await sink.send(_StubClient(), payload)

    assert captured["headers"]["Priority"] == "max"


@pytest.mark.asyncio
async def test_ntfy_sink_quiet_hours_overrides_max(monkeypatch):
    """Even a max-relevance posting goes to low during quiet hours."""
    from src.notify.ntfy import NtfySink

    captured: dict = {}

    class _StubClient:
        async def post(self, url, *, content, headers, timeout):
            captured["headers"] = headers

            class _Resp:
                def raise_for_status(self): pass
            return _Resp()

    qh = QuietHoursConfig(timezone=ZoneInfo("UTC"), start=time(23, 0), end=time(7, 0))
    # Pin to 02:00 UTC — inside quiet hours
    monkeypatch.setattr(
        "src.notify.ntfy.datetime",
        type("D", (), {
            "now": staticmethod(lambda tz=None: __import__("datetime").datetime(2026, 5, 1, 2, 0, tzinfo=tz)),
        })(),
    )
    sink = NtfySink(topic_url="https://ntfy.test/x", quiet_hours=qh)
    payload = NotificationPayload(
        title="X @ Y", company="Y", role="X", location="Remote", comp=None,
        stack_matched=[], posted="now", apply_url="https://x", source="src",
        seniority="senior", tags=[],
        relevance_score=9, relevance_rationale="Strong", relevance_priority="max",
    )
    await sink.send(_StubClient(), payload)

    assert captured["headers"]["Priority"] == "low"


@pytest.mark.asyncio
async def test_ntfy_sink_includes_score_in_title(monkeypatch):
    from src.notify.ntfy import NtfySink

    captured: dict = {}

    class _StubClient:
        async def post(self, url, *, content, headers, timeout):
            captured["headers"] = headers

            class _Resp:
                def raise_for_status(self): pass
            return _Resp()

    qh = QuietHoursConfig(timezone=ZoneInfo("UTC"), start=time(23, 0), end=time(7, 0))
    monkeypatch.setattr(
        "src.notify.ntfy.datetime",
        type("D", (), {
            "now": staticmethod(lambda tz=None: __import__("datetime").datetime(2026, 5, 1, 12, 0, tzinfo=tz)),
        })(),
    )
    sink = NtfySink(topic_url="https://ntfy.test/x", quiet_hours=qh)
    payload = NotificationPayload(
        title="X @ Y", company="Y", role="X", location="Remote", comp=None,
        stack_matched=[], posted="now", apply_url="https://x", source="src",
        seniority="senior", tags=[],
        relevance_score=8, relevance_rationale="Solid", relevance_priority="default",
    )
    await sink.send(_StubClient(), payload)

    assert captured["headers"]["Title"].startswith("[8/10] ")


@pytest.mark.asyncio
async def test_ntfy_adds_actions_header_when_tailor_url():
    sink = NtfySink(topic_url="https://ntfy.sh/test", quiet_hours=QH)
    p = NotificationPayload(**{**PAYLOAD.__dict__, "tailor_url": "https://ep.example?job_id=j&t=tok"})
    async with httpx.AsyncClient() as client:
        with respx.mock:
            route = respx.post("https://ntfy.sh/test").respond(200)
            await sink.send(client, p)
        assert route.calls.last.request.headers["Actions"] == "view, Tailor resume, https://ep.example?job_id=j&t=tok"


@pytest.mark.asyncio
async def test_ntfy_no_actions_header_without_tailor_url():
    sink = NtfySink(topic_url="https://ntfy.sh/test", quiet_hours=QH)
    async with httpx.AsyncClient() as client:
        with respx.mock:
            route = respx.post("https://ntfy.sh/test").respond(200)
            await sink.send(client, PAYLOAD)   # tailor_url defaults to None
        assert "Actions" not in route.calls.last.request.headers


@freeze_time("2026-04-30 18:00:00", tz_offset=0)
@pytest.mark.asyncio
async def test_ntfy_adzuna_attribution_line():
    payload = NotificationPayload(**{**PAYLOAD.__dict__, "source": "adzuna"})
    sink = NtfySink(topic_url="https://ntfy.sh/test", quiet_hours=QH)
    async with httpx.AsyncClient() as client:
        with respx.mock:
            route = respx.post("https://ntfy.sh/test").respond(200)
            await sink.send(client, payload)
    body = route.calls.last.request.content.decode()
    assert "Jobs by Adzuna — https://www.adzuna.com" in body


@freeze_time("2026-04-30 18:00:00", tz_offset=0)
@pytest.mark.asyncio
async def test_ntfy_no_attribution_for_other_sources():
    sink = NtfySink(topic_url="https://ntfy.sh/test", quiet_hours=QH)
    async with httpx.AsyncClient() as client:
        with respx.mock:
            route = respx.post("https://ntfy.sh/test").respond(200)
            await sink.send(client, PAYLOAD)
    assert "Adzuna" not in route.calls.last.request.content.decode()


@freeze_time("2026-04-30 09:00:00", tz_offset=0)  # 02:00 PT — inside QH's window
@pytest.mark.asyncio
async def test_ntfy_without_quiet_hours_never_lowers_priority():
    sink = NtfySink(topic_url="https://ntfy.sh/test", quiet_hours=None)
    async with httpx.AsyncClient() as client:
        with respx.mock:
            route = respx.post("https://ntfy.sh/test").respond(200)
            await sink.send(client, PAYLOAD)
        assert route.calls.last.request.headers["Priority"] == "default"
