# tests/web/test_watchdog.py
import httpx
import pytest
from fastapi.testclient import TestClient

from src.ops_alerts import OpsThresholds
from src.sqlite_db import connect
from src.state_sqlite import _now_ms
from src.web.app import create_app
from src.web.repo import TriageRepo
from src.web.watchdog import check_once
from tests.sqlite_helpers import sqlite_stores


def _sqlite_stores():
    conn = connect(":memory:")
    return conn, sqlite_stores(conn)


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


def test_watchdog_inert_without_ops_env(monkeypatch, tmp_path):
    monkeypatch.setenv("JOB_AGG_TAILORED_DIR", str(tmp_path / "tailored"))
    monkeypatch.delenv("JOB_AGG_OPS_NTFY_TOPIC_URL", raising=False)
    monkeypatch.delenv("JOB_AGG_OPS_DISCORD_WEBHOOK_URL", raising=False)
    _, stores = _sqlite_stores()
    app = create_app(repo=TriageRepo(stores.seen), stores=stores)
    with TestClient(app):
        assert getattr(app.state, "watchdog_task", None) is None


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_watchdog_starts_with_ops_env(monkeypatch, tmp_path):
    monkeypatch.setenv("JOB_AGG_TAILORED_DIR", str(tmp_path / "tailored"))
    monkeypatch.setenv("JOB_AGG_OPS_NTFY_TOPIC_URL", "https://ntfy.test/ops")
    _, stores = _sqlite_stores()
    app = create_app(repo=TriageRepo(stores.seen), stores=stores)
    with TestClient(app):
        task = getattr(app.state, "watchdog_task", None)
        assert task is not None and not task.done()
    # shutdown cancelled it
    assert app.state.watchdog_task.cancelled() or app.state.watchdog_task.done()
