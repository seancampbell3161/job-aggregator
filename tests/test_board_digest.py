# tests/test_board_digest.py
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

from src.board_digest import compose_board_digest, send_board_digest

NOW = datetime(2026, 7, 2, 15, 0, tzinfo=timezone.utc)


def _card(company, status, *, days_in_stage=0, closed_at=None, notified=False, applied_on=None):
    since = (NOW - timedelta(days=days_in_stage)).isoformat()
    history = [{"status": status, "at": since}]
    if applied_on:
        history.insert(0, {"status": "applied", "at": applied_on})
    return {
        "job_id": f"x:{company.lower()}", "company": company, "title": "Eng",
        "status": status, "history": history, "first_seen": since,
        "posting_closed_at": closed_at, "closed_notified": notified,
    }


def test_compose_none_when_nothing_to_say():
    cards = [
        _card("Fresh", "applied", days_in_stage=2),
        _card("Quiet", "interested", days_in_stage=30),          # interested never stales
        _card("Done", "applied", days_in_stage=3,
              closed_at="2026-07-01T04:30:00+00:00", notified=True),  # already announced
    ]
    assert compose_board_digest(cards, stale_after_days=10, now=NOW) is None


def test_compose_stale_and_closed():
    cards = [
        _card("Acme", "applied", days_in_stage=14),
        _card("BigCo", "interviewing", days_in_stage=11),
        _card("Widget Inc", "applied", days_in_stage=3,
              closed_at="2026-07-02T04:30:00+00:00",
              applied_on="2026-06-20T12:00:00+00:00"),
    ]
    result = compose_board_digest(cards, stale_after_days=10, now=NOW)
    assert result is not None
    message, to_mark = result
    assert "2 stale" in message
    assert "Acme (14d applied)" in message
    assert "BigCo (11d interviewing)" in message
    assert "1 posting closed: Widget Inc" in message
    assert to_mark == ["x:widget inc"]


def test_compose_stale_reannounces_but_closed_does_not():
    cards = [_card("Acme", "applied", days_in_stage=14)]
    r1 = compose_board_digest(cards, stale_after_days=10, now=NOW)
    r2 = compose_board_digest(cards, stale_after_days=10, now=NOW)
    assert r1 is not None and r2 is not None  # staleness nags daily by design


@pytest.mark.asyncio
async def test_send_both_sinks_and_click_header():
    seen = []
    def h(request):
        seen.append(request)
        return httpx.Response(200)
    async with httpx.AsyncClient(transport=httpx.MockTransport(h)) as client:
        ok = await send_board_digest(
            client, "Board: 1 stale — Acme (14d applied)",
            ntfy_topic_url="https://ntfy.test/jobs",
            discord_webhook_url="https://discord.test/jobs",
            click_url="http://stack:8000/board",
        )
    assert ok is True
    assert [str(r.url) for r in seen] == ["https://ntfy.test/jobs", "https://discord.test/jobs"]
    ntfy = seen[0]
    assert ntfy.headers["Title"] == "Board digest"
    assert ntfy.headers["Click"] == "http://stack:8000/board"
    ntfy.headers["Title"].encode("latin-1")  # must not raise


@pytest.mark.asyncio
async def test_send_no_sinks_returns_false_and_no_click_when_unset():
    calls = []
    def h(request):
        calls.append(request)
        return httpx.Response(200)
    async with httpx.AsyncClient(transport=httpx.MockTransport(h)) as client:
        assert await send_board_digest(client, "x") is False
        await send_board_digest(client, "x", ntfy_topic_url="https://ntfy.test/jobs")
    assert "Click" not in calls[0].headers


def _suggested(company="Widget Inc"):
    c = _card(company, "applied", days_in_stage=2)  # 2d in stage: not stale
    c["email_suggestion"] = {"suggested_status": "rejected", "message_id": "<r1@ats>"}
    return c


def test_digest_appends_pending_suggestion_count():
    cards = [_card("Acme", "applied", days_in_stage=14), _suggested()]
    message, to_mark = compose_board_digest(cards, stale_after_days=10, now=NOW)
    assert "1 stale" in message
    assert "1 email suggestion pending" in message
    assert to_mark == []  # to-mark stays closures-only


def test_digest_suggestions_alone_trigger_message():
    result = compose_board_digest([_suggested()], stale_after_days=10, now=NOW)
    assert result is not None
    message, to_mark = result
    assert message == "Board: 1 email suggestion pending"
    assert to_mark == []


def test_digest_plural_suggestions():
    cards = [_suggested("Acme"), _suggested("BigCo")]
    message, _ = compose_board_digest(cards, stale_after_days=10, now=NOW)
    assert "2 email suggestions pending" in message


def test_digest_ignores_suggestions_on_archived_cards():
    c = _suggested()
    c["status"] = "rejected"
    assert compose_board_digest([c], stale_after_days=10, now=NOW) is None
