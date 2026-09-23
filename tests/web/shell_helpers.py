"""Helpers for the app-shell / Home tests: a real app over a per-test SQLite
file, plus seeders for matches and pipeline cycles."""
from __future__ import annotations

import time
from datetime import datetime, timezone

from src.models import NormalizedPosting
from src.web.app import create_app
from tests.auth_helpers import signed_in_client
from tests.settings_helpers import WEB_TEST_SETTINGS, make_service


def make_app(tmp_path, monkeypatch, *, secrets=None, settings=None, documents=None):
    monkeypatch.setenv("JOB_AGG_SQLITE_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("JOB_AGG_TAILORED_DIR", str(tmp_path / "tailored"))
    return create_app(service=make_service(
        WEB_TEST_SETTINGS if settings is None else settings,
        documents=documents, secrets=secrets,
    ))


def client_for(app, **kw):
    return signed_in_client(app, **kw)


def seed_match(app, job_id, *, status=None, title="Engineer"):
    store = app.state.stores.seen
    store.claim_for_notify(
        job_id, score=7, rationale="r", gaps=[],
        posting=NormalizedPosting(
            job_id=job_id, title=title, company="Acme", location_text="Remote",
            location_tags=frozenset(), seniority="senior", stack=frozenset(),
            comp_min=None, comp_max=None, apply_url=f"https://a/{job_id}",
            description="", posted_at=datetime(2026, 9, 1, tzinfo=timezone.utc), source="acme",
        ),
    )
    if status:
        store.set_status(job_id, status)


def seed_cycle(app, *, minutes_ago, tier="ats", ok=True):
    ts = int(time.time() * 1000) - int(minutes_ago * 60_000)
    app.state.stores.events._conn.execute(
        "INSERT INTO pipeline_events (ts_ms, tier, fetched, matched, notified, duration_ms, ok, failures, llm_failures) "
        "VALUES (?, ?, 0, 0, 0, 0, ?, '[]', '[]')", (ts, tier, 1 if ok else 0))


def finish_wizard(app):
    """Skip every wizard step, so next_step() is None."""
    from src.web.wizard.steps import WIZARD_STEPS
    for step in WIZARD_STEPS:
        app.state.stores.wizard.skip(step.slug)
