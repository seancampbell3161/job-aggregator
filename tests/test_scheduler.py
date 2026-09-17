"""Tests for the APScheduler daemon (src/scheduler.py)."""
from datetime import datetime, timezone

from src.models import NormalizedPosting
from tests.settings_helpers import make_service, seed_settings

ALL_JOBS = {
    "ats", "slow", "discovery", "headless", "digest", "prune", "closed_check",
    "board_digest", "integrity", "gmail_check", "config_watch",
}


def _posting(job_id, *, title="Eng", company="Widget"):
    return NormalizedPosting(
        job_id=job_id, title=title, company=company, location_text="x",
        location_tags=frozenset(), seniority="mid", stack=frozenset(),
        comp_min=None, comp_max=None, apply_url=f"https://a/{job_id}", description="d",
        posted_at=datetime(2026, 7, 1, tzinfo=timezone.utc), source="a",
    )


def test_prune_sweeps_rejected_postings():
    from src.scheduler import _prune
    from src.stores import build_stores
    service = seed_settings({"audit": {"retention_days": 30}})
    stores = build_stores()
    stores.rejected.record(_posting("a:1", title="Office Manager", company="Acme"), rejected_by="role")
    stores.rejected._conn.execute(
        "UPDATE rejected_postings SET first_seen = '2020-01-01T00:00:00+00:00'"
    )
    _prune(service)
    assert build_stores().rejected.list_rejected(since_iso="") == []


def test_build_scheduler_registers_every_job_when_configured():
    from src.scheduler import build_scheduler
    sched = build_scheduler(make_service({}))
    assert {j.id for j in sched.get_jobs()} == ALL_JOBS
    for j in sched.get_jobs():
        assert j.max_instances == 1


def test_build_scheduler_registers_every_job_when_not_set_up():
    from src.scheduler import build_scheduler
    sched = build_scheduler(make_service())
    assert {j.id for j in sched.get_jobs()} == ALL_JOBS
    assert str(sched.get_job("ats").trigger) == "interval[0:10:00]"  # model default
    assert str(sched.get_job("config_watch").trigger) == "interval[0:00:30]"


def test_build_scheduler_uses_settings_triggers():
    from src.scheduler import build_scheduler
    sched = build_scheduler(make_service({
        "schedules": {"ats_minutes": 2},
        "board": {"closed_check_cron": "15 3 * * *"},
    }))
    assert str(sched.get_job("ats").trigger) == "interval[0:02:00]"
    assert "hour='3'" in str(sched.get_job("closed_check").trigger)


def test_config_watch_reschedules_exactly_the_changed_jobs():
    from src.scheduler import build_scheduler
    service = make_service({})
    sched = build_scheduler(service)
    before = {j.id: str(j.trigger) for j in sched.get_jobs()}
    service.save_settings(
        {"schedules": {"ats_minutes": 3}, "board": {"digest_cron": "0 16 * * *"}}, source="cli",
    )
    watch = sched.get_job("config_watch")
    watch.func(*watch.args)
    after = {j.id: str(j.trigger) for j in sched.get_jobs()}
    assert {k for k in before if before[k] != after[k]} == {"ats", "board_digest"}
    assert after["ats"] == "interval[0:03:00]"

    calls = []
    real = sched.reschedule_job
    sched.reschedule_job = lambda *a, **k: calls.append(a) or real(*a, **k)
    watch.func(*watch.args)  # nothing changed since the last pass
    assert calls == []


def test_config_watch_survives_a_failing_reschedule_and_retries(caplog):
    from src.scheduler import build_scheduler
    service = make_service({})
    sched = build_scheduler(service)
    service.save_settings({"schedules": {"ats_minutes": 3, "slow_minutes": 7}}, source="cli")
    real = sched.reschedule_job

    def flaky(job_id, **kwargs):
        if job_id == "ats":
            raise RuntimeError("boom")
        return real(job_id, **kwargs)

    sched.reschedule_job = flaky
    watch = sched.get_job("config_watch")
    with caplog.at_level("ERROR", logger="src.scheduler"):
        watch.func(*watch.args)
    assert str(sched.get_job("slow").trigger) == "interval[0:07:00]"
    assert any(r.message == "job_reschedule_failed" for r in caplog.records)
    sched.reschedule_job = real
    watch.func(*watch.args)
    assert str(sched.get_job("ats").trigger) == "interval[0:03:00]"


def test_jobs_are_noops_when_not_set_up(monkeypatch):
    import src.scheduler as scheduler
    service = make_service()
    called = []
    monkeypatch.setattr(scheduler, "build_stores", lambda *a, **k: called.append("stores"))
    monkeypatch.setattr(scheduler, "run_gmail_check", lambda *a, **k: called.append("gmail"))
    scheduler._prune(service)
    scheduler._closed_check(service)
    scheduler._board_digest(service)
    scheduler._gmail_check(service)
    assert called == []


def test_tick_passes_the_service_to_run(monkeypatch):
    import src.scheduler as scheduler
    service = make_service({})
    seen = {}

    async def fake_run(*, tier, service):
        seen.update(tier=tier, service=service)
        return {}

    monkeypatch.setattr(scheduler, "_run", fake_run)
    scheduler._tick(service, "slow")
    assert seen == {"tier": "slow", "service": service}


def test_integrity_check_runs_repair_on_sqlite(monkeypatch):
    """The hourly maintenance job invokes integrity_check_and_repair on a fresh
    SQLite connection."""
    import src.scheduler as scheduler

    calls = {"checked": 0, "closed": 0}

    class _Conn:
        def close(self):
            calls["closed"] += 1

    monkeypatch.setattr("src.sqlite_db.connect", lambda *a, **k: _Conn())
    monkeypatch.setattr(
        "src.sqlite_db.integrity_check_and_repair",
        lambda conn: calls.__setitem__("checked", calls["checked"] + 1),
    )
    scheduler._integrity_check()
    assert calls == {"checked": 1, "closed": 1}


def test_board_digest_job_marks_notified_only_on_successful_send(monkeypatch):
    from src.scheduler import _board_digest
    from src.stores import build_stores
    monkeypatch.setenv("JOB_AGG_NTFY_TOPIC_URL", "https://ntfy.test/x")
    monkeypatch.setenv("JOB_AGG_DISCORD_WEBHOOK_URL", "https://discord.test/x")
    service = seed_settings({})
    store = build_stores().seen
    store.claim_for_notify("a:1", score=7, posting=_posting("a:1"))
    store.set_status("a:1", "applied")
    store.update_closed_check("a:1", misses=2, closed_at="2026-07-02T04:30:00+00:00")

    async def send_fails(*args, **kwargs):
        return False

    monkeypatch.setattr("src.scheduler.send_board_digest", send_fails)
    _board_digest(service)
    assert build_stores().seen.get_match("a:1")["closed_notified"] is False

    async def send_ok(*args, **kwargs):
        return True

    monkeypatch.setattr("src.scheduler.send_board_digest", send_ok)
    _board_digest(service)
    assert build_stores().seen.get_match("a:1")["closed_notified"] is True


def test_gmail_check_job_skips_without_secrets(monkeypatch):
    import src.scheduler as scheduler
    monkeypatch.delenv("JOB_AGG_GMAIL_ADDRESS", raising=False)
    monkeypatch.delenv("JOB_AGG_GMAIL_APP_PASSWORD", raising=False)
    service = seed_settings({})
    called = {"n": 0}
    monkeypatch.setattr(
        scheduler, "run_gmail_check",
        lambda *a, **k: called.__setitem__("n", called["n"] + 1),
    )
    scheduler._gmail_check(service)  # must not raise, must not run
    assert called["n"] == 0


def test_gmail_check_job_runs_with_secrets(monkeypatch):
    import src.scheduler as scheduler
    monkeypatch.setenv("JOB_AGG_GMAIL_ADDRESS", "me@gmail.com")
    monkeypatch.setenv("JOB_AGG_GMAIL_APP_PASSWORD", "pw")
    service = seed_settings({"gmail": {"max_messages_per_run": 50}})
    seen_kwargs = {}

    def fake_run(store, source_state, **kwargs):
        seen_kwargs.update(kwargs)
        return {"fetched": 0, "matched": 0, "suggested": 0, "skipped": 0}

    monkeypatch.setattr(scheduler, "run_gmail_check", fake_run)
    scheduler._gmail_check(service)
    assert seen_kwargs["address"] == "me@gmail.com"
    assert seen_kwargs["max_messages"] == 50
