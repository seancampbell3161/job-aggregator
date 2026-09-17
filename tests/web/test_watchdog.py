# tests/web/test_watchdog.py
import httpx
import pytest

from src.ops_alerts import OpsThresholds
from src.sqlite_db import connect
from src.state_sqlite import _now_ms
from src.web.app import create_app
from src.web.repo import TriageRepo
from src.web.watchdog import check_once, watchdog_pass
from tests.auth_helpers import signed_in_client
from tests.settings_helpers import make_service
from tests.sqlite_helpers import sqlite_stores


def _sqlite_stores():
    conn = connect(":memory:")
    return conn, sqlite_stores(conn)


def _stale_ats_cycle(conn, minutes_ago=25):
    conn.execute(
        "INSERT INTO pipeline_events (ts_ms, tier, fetched, matched, notified, duration_ms, ok, failures, llm_failures) "
        "VALUES (?, 'ats', 1, 0, 0, 1, 1, '[]', '[]')",
        (_now_ms() - minutes_ago * 60_000,),
    )


def _recording_factory(sent: list[str]):
    def handler(request):
        sent.append(request.headers.get("Title", ""))
        return httpx.Response(200)
    return lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_check_once_sends_stale_alert_then_recovery():
    conn, stores = _sqlite_stores()
    conn.execute(
        "INSERT INTO pipeline_events (ts_ms, tier, fetched, matched, notified, duration_ms, ok, failures, llm_failures) "
        "VALUES (?, 'ats', 1, 0, 0, 1, 1, '[]', '[]')",
        (_now_ms() - 25 * 60_000,),  # 25 min ago; threshold max(3*2, 15) = 15 min
    )
    sent: list[str] = []
    def h(request):
        sent.append(request.headers.get("Title", ""))
        return httpx.Response(200)
    factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(h))
    assert await check_once(
        events=stores.events, alert_state=stores.alert_state, ats_minutes=2,
        thresholds=OpsThresholds(), ntfy_topic_url="https://ntfy.test/ops",
        discord_webhook_url="", client_factory=factory,
    ) is True
    assert sent == ["pipeline stopped"]
    # a fresh cycle appears -> recovery notice
    stores.events.record_cycle(tier="ats", fetched=1, matched=0, notified=0, duration_ms=1)
    assert await check_once(
        events=stores.events, alert_state=stores.alert_state, ats_minutes=2,
        thresholds=OpsThresholds(), ntfy_topic_url="https://ntfy.test/ops",
        discord_webhook_url="", client_factory=factory,
    ) is True
    assert sent[-1].startswith("recovered")


@pytest.mark.asyncio
async def test_check_once_rearms_alert_when_send_fails():
    conn, stores = _sqlite_stores()
    conn.execute(
        "INSERT INTO pipeline_events (ts_ms, tier, fetched, matched, notified, duration_ms, ok, failures, llm_failures) "
        "VALUES (?, 'ats', 1, 0, 0, 1, 1, '[]', '[]')",
        (_now_ms() - 25 * 60_000,),  # 25 min ago; threshold max(3*2, 15) = 15 min
    )
    failing_factory = lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(500))
    )
    assert await check_once(
        events=stores.events, alert_state=stores.alert_state, ats_minutes=2,
        thresholds=OpsThresholds(), ntfy_topic_url="https://ntfy.test/ops",
        discord_webhook_url="", client_factory=failing_factory,
    ) is False
    # Every sink failed: the condition must have re-armed rather than sitting
    # in cooldown, so a subsequent check with a working sink sends right away.
    sent: list[str] = []
    def h(request):
        sent.append(request.headers.get("Title", ""))
        return httpx.Response(200)
    working_factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(h))
    assert await check_once(
        events=stores.events, alert_state=stores.alert_state, ats_minutes=2,
        thresholds=OpsThresholds(), ntfy_topic_url="https://ntfy.test/ops",
        discord_webhook_url="", client_factory=working_factory,
    ) is True
    assert sent == ["pipeline stopped"]


def test_watchdog_task_runs_with_the_app_lifespan(monkeypatch, tmp_path):
    monkeypatch.setenv("JOB_AGG_TAILORED_DIR", str(tmp_path / "tailored"))
    _, stores = _sqlite_stores()
    app = create_app(repo=TriageRepo(stores.seen), stores=stores, service=make_service())
    with signed_in_client(app):
        task = app.state.watchdog_task
        assert task is not None and not task.done()
    assert task.cancelled() or task.done()


@pytest.mark.asyncio
async def test_watchdog_pass_is_inert_until_setup_and_an_ops_sink():
    conn, stores = _sqlite_stores()
    _stale_ats_cycle(conn)
    sent: list[str] = []
    for service in (make_service(), make_service({"schedules": {"ats_minutes": 2}})):
        assert await watchdog_pass(
            service=service, events=stores.events, alert_state=stores.alert_state,
            client_factory=_recording_factory(sent),
        ) is False
    assert sent == []


@pytest.mark.asyncio
async def test_watchdog_pass_reads_sinks_and_cadence_from_settings():
    conn, stores = _sqlite_stores()
    _stale_ats_cycle(conn)  # 25 min ago; ats_minutes=2 → threshold max(3*2, 15) = 15 min
    sent: list[str] = []
    service = make_service({"schedules": {"ats_minutes": 2}},
                           secrets={"ops_ntfy_topic_url": "https://ntfy.test/ops"})
    assert await watchdog_pass(
        service=service, events=stores.events, alert_state=stores.alert_state,
        client_factory=_recording_factory(sent),
    ) is True
    assert "pipeline stopped" in sent


@pytest.mark.asyncio
async def test_watchdog_pass_uses_the_configured_cadence():
    conn, stores = _sqlite_stores()
    _stale_ats_cycle(conn)  # 25 min ago; default ats_minutes=10 → threshold 30 min: not stale
    sent: list[str] = []
    service = make_service({}, secrets={"ops_ntfy_topic_url": "https://ntfy.test/ops"})
    assert await watchdog_pass(
        service=service, events=stores.events, alert_state=stores.alert_state,
        client_factory=_recording_factory(sent),
    ) is False
    assert sent == []
