"""Tests for the APScheduler daemon (src/scheduler.py)."""


def test_prune_sweeps_rejected_postings(monkeypatch, tmp_path):
    from datetime import datetime, timezone
    monkeypatch.setenv("JOB_AGG_SQLITE_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("JOB_AGG_NTFY_TOPIC_URL", "https://ntfy.test/x")
    monkeypatch.setenv("JOB_AGG_DISCORD_WEBHOOK_URL", "https://discord.test/x")
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "filters:\n"
        "  titles: [software engineer]\n"
        "  seniority_allow: [mid]\n"
        "  location: {remote_must_be_us: true}\n"
        "  comp_floor_usd: 0\n"
        "  stack_any_of: [python]\n"
        "quiet_hours: {timezone: UTC, start: '23:00', end: '07:00'}\n"
        "sources: {}\n"
        "schedules: {ats_minutes: 2, slow_minutes: 15}\n"
        "audit: {retention_days: 30}\n"
    )
    from src.config import load_config
    from src.models import NormalizedPosting
    from src.scheduler import _prune
    from src.stores import build_stores
    cfg = load_config(cfg_path)
    stores = build_stores(cfg)
    posting = NormalizedPosting(
        job_id="a:1", title="Office Manager", company="Acme", location_text="x",
        location_tags=frozenset(), seniority="mid", stack=frozenset(),
        comp_min=None, comp_max=None, apply_url="https://a/1", description="d",
        posted_at=datetime(2026, 7, 1, tzinfo=timezone.utc), source="a",
    )
    stores.rejected.record(posting, rejected_by="role")
    stores.rejected._conn.execute(
        "UPDATE rejected_postings SET first_seen = '2020-01-01T00:00:00+00:00'"
    )
    _prune(cfg)
    assert build_stores(cfg).rejected.list_rejected(since_iso="") == []


def test_build_scheduler_registers_all_tiers(monkeypatch):
    monkeypatch.setenv("JOB_AGG_CONFIG_PATH", "config.example.yaml")
    from src.config import load_config
    from src.scheduler import build_scheduler

    # secrets are required by load_config; set placeholders
    monkeypatch.setenv("JOB_AGG_NTFY_TOPIC_URL", "https://ntfy.sh/x")
    monkeypatch.setenv("JOB_AGG_DISCORD_WEBHOOK_URL", "https://discord/x")
    cfg = load_config("config.example.yaml")
    sched = build_scheduler(cfg)
    ids = {j.id for j in sched.get_jobs()}
    assert ids == {"ats", "slow", "discovery", "headless", "digest", "prune", "closed_check", "board_digest", "integrity", "gmail_check"}
    for j in sched.get_jobs():
        assert j.max_instances == 1


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


def test_build_scheduler_registers_closed_check(monkeypatch, tmp_path):
    from src.config import load_config
    from src.scheduler import build_scheduler
    monkeypatch.setenv("JOB_AGG_NTFY_TOPIC_URL", "https://ntfy.test/x")
    monkeypatch.setenv("JOB_AGG_DISCORD_WEBHOOK_URL", "https://discord.test/x")
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "filters:\n"
        "  titles: [software engineer]\n"
        "  seniority_allow: [mid]\n"
        "  location: {remote_must_be_us: true}\n"
        "  comp_floor_usd: 0\n"
        "  stack_any_of: [python]\n"
        "quiet_hours: {timezone: UTC, start: '23:00', end: '07:00'}\n"
        "sources: {}\n"
        "schedules: {ats_minutes: 2, slow_minutes: 15}\n"
    )
    sched = build_scheduler(load_config(cfg_path))
    assert {"closed_check", "board_digest"} <= {j.id for j in sched.get_jobs()}


def test_board_digest_job_marks_notified_only_on_successful_send(monkeypatch, tmp_path):
    from datetime import datetime, timezone
    from src.config import load_config
    from src.models import NormalizedPosting
    from src.scheduler import _board_digest
    from src.stores import build_stores

    monkeypatch.setenv("JOB_AGG_SQLITE_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("JOB_AGG_NTFY_TOPIC_URL", "https://ntfy.test/x")
    monkeypatch.setenv("JOB_AGG_DISCORD_WEBHOOK_URL", "https://discord.test/x")
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "filters:\n"
        "  titles: [software engineer]\n"
        "  seniority_allow: [mid]\n"
        "  location: {remote_must_be_us: true}\n"
        "  comp_floor_usd: 0\n"
        "  stack_any_of: [python]\n"
        "quiet_hours: {timezone: UTC, start: '23:00', end: '07:00'}\n"
        "sources: {}\n"
        "schedules: {ats_minutes: 2, slow_minutes: 15}\n"
    )
    cfg = load_config(cfg_path)
    store = build_stores(cfg).seen
    posting = NormalizedPosting(
        job_id="a:1", title="Eng", company="Widget", location_text="x",
        location_tags=frozenset(), seniority="mid", stack=frozenset(),
        comp_min=None, comp_max=None, apply_url="https://a/1", description="d",
        posted_at=datetime(2026, 7, 1, tzinfo=timezone.utc), source="a",
    )
    store.claim_for_notify("a:1", score=7, posting=posting)
    store.set_status("a:1", "applied")
    store.update_closed_check("a:1", misses=2, closed_at="2026-07-02T04:30:00+00:00")

    async def send_fails(*args, **kwargs):
        return False

    monkeypatch.setattr("src.scheduler.send_board_digest", send_fails)
    _board_digest(cfg)
    assert build_stores(cfg).seen.get_match("a:1")["closed_notified"] is False

    async def send_ok(*args, **kwargs):
        return True

    monkeypatch.setattr("src.scheduler.send_board_digest", send_ok)
    _board_digest(cfg)
    assert build_stores(cfg).seen.get_match("a:1")["closed_notified"] is True


def test_gmail_check_job_skips_without_secrets(monkeypatch, tmp_path):
    import src.scheduler as scheduler

    monkeypatch.setenv("JOB_AGG_SQLITE_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("JOB_AGG_NTFY_TOPIC_URL", "https://ntfy.test/x")
    monkeypatch.setenv("JOB_AGG_DISCORD_WEBHOOK_URL", "https://discord.test/x")
    monkeypatch.delenv("JOB_AGG_GMAIL_ADDRESS", raising=False)
    monkeypatch.delenv("JOB_AGG_GMAIL_APP_PASSWORD", raising=False)
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "filters:\n"
        "  titles: [software engineer]\n"
        "  seniority_allow: [mid]\n"
        "  location: {remote_must_be_us: true}\n"
        "  comp_floor_usd: 0\n"
        "  stack_any_of: [python]\n"
        "quiet_hours: {timezone: UTC, start: '23:00', end: '07:00'}\n"
        "sources: {}\n"
        "schedules: {ats_minutes: 2, slow_minutes: 15}\n"
    )
    from src.config import load_config

    called = {"n": 0}
    monkeypatch.setattr(
        scheduler, "run_gmail_check",
        lambda *a, **k: called.__setitem__("n", called["n"] + 1),
    )
    scheduler._gmail_check(load_config(cfg_path))  # must not raise, must not run
    assert called["n"] == 0


def test_gmail_check_job_runs_with_secrets(monkeypatch, tmp_path):
    import src.scheduler as scheduler

    monkeypatch.setenv("JOB_AGG_SQLITE_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("JOB_AGG_NTFY_TOPIC_URL", "https://ntfy.test/x")
    monkeypatch.setenv("JOB_AGG_DISCORD_WEBHOOK_URL", "https://discord.test/x")
    monkeypatch.setenv("JOB_AGG_GMAIL_ADDRESS", "me@gmail.com")
    monkeypatch.setenv("JOB_AGG_GMAIL_APP_PASSWORD", "pw")
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "filters:\n"
        "  titles: [software engineer]\n"
        "  seniority_allow: [mid]\n"
        "  location: {remote_must_be_us: true}\n"
        "  comp_floor_usd: 0\n"
        "  stack_any_of: [python]\n"
        "quiet_hours: {timezone: UTC, start: '23:00', end: '07:00'}\n"
        "sources: {}\n"
        "schedules: {ats_minutes: 2, slow_minutes: 15}\n"
        "gmail: {max_messages_per_run: 50}\n"
    )
    from src.config import load_config

    seen_kwargs = {}

    def fake_run(store, source_state, **kwargs):
        seen_kwargs.update(kwargs)
        return {"fetched": 0, "matched": 0, "suggested": 0, "skipped": 0}

    monkeypatch.setattr(scheduler, "run_gmail_check", fake_run)
    scheduler._gmail_check(load_config(cfg_path))
    assert seen_kwargs["address"] == "me@gmail.com"
    assert seen_kwargs["max_messages"] == 50
