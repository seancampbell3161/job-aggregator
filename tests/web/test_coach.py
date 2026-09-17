# tests/web/test_coach.py
from datetime import datetime, timezone

import pytest

from src.coach import CoachCard, CoachResult
from src.models import NormalizedPosting
from src.sqlite_db import connect
from src.state_sqlite import (
    SqliteCoachRunsStore,
    SqliteRejectedPostingsStore,
    SqliteSeenJobsStore,
)

_CARD = CoachCard(category="filters", title="Widen titles",
                  evidence="0 of 12 staff", action="add staff", impact="high")


class _FakeEngine:
    def __init__(self, result: CoachResult):
        self._result = result
        self.calls = 0

    async def recommend(self, snapshot) -> CoachResult:
        self.calls += 1
        self.last_snapshot = snapshot
        return self._result


def _posting(job_id, title="Backend Engineer", company="Acme"):
    return NormalizedPosting(
        job_id=job_id, title=title, company=company, location_text="Remote (US)",
        location_tags=frozenset({"remote", "us"}), seniority="mid",
        stack=frozenset({"python"}), comp_min=None, comp_max=None,
        apply_url=f"https://apply/{job_id}", description="Ship Python services.",
        posted_at=datetime(2026, 7, 1, tzinfo=timezone.utc), source="greenhouse:acme",
    )


@pytest.fixture
def stores_trio():
    conn = connect(":memory:")
    seen = SqliteSeenJobsStore(conn)
    rejected = SqliteRejectedPostingsStore(conn)
    coach_store = SqliteCoachRunsStore(conn)
    seen.claim_for_notify("greenhouse:acme:1", score=8, posting=_posting("greenhouse:acme:1"))
    seen.set_status("greenhouse:acme:1", "applied")
    rejected.record(_posting("greenhouse:acme:2", title="Office Manager"), rejected_by="role")
    return seen, rejected, coach_store


def _provider(stores_trio, engine=None, store=True):
    from src.web.coach import CoachProvider
    seen, rejected, coach_store = stores_trio
    return CoachProvider(
        store=coach_store if store else None, seen=seen, rejected=rejected,
        engine=engine, provider_name="fake", model_name="fake-1",
    )


@pytest.mark.asyncio
async def test_run_persists_ok_run(stores_trio):
    engine = _FakeEngine(CoachResult(cards=[_CARD], is_fallback=False))
    prov = _provider(stores_trio, engine=engine)
    run = await prov.run()
    assert run["status"] == "ok"
    assert run["cards"][0]["title"] == "Widen titles"
    assert run["provider"] == "fake" and run["model"] == "fake-1"
    assert prov.latest()["run_id"] == run["run_id"]
    # the engine saw a real snapshot: 1 match, applied
    assert engine.last_snapshot.meta["applied_count"] == 1
    assert engine.last_snapshot.aggregates["audit"]["rejected_by_gate"] == {"role": 1}


@pytest.mark.asyncio
async def test_run_persists_error_run_on_fallback(stores_trio):
    engine = _FakeEngine(CoachResult(cards=[], is_fallback=True, error_type="ConnectError"))
    prov = _provider(stores_trio, engine=engine)
    run = await prov.run()
    assert run["status"] == "error" and run["error"] == "ConnectError"
    assert run["cards"] == []


@pytest.mark.asyncio
async def test_run_without_engine_or_store_returns_none(stores_trio):
    prov = _provider(stores_trio, engine=None)
    assert prov.can_run is False
    assert await prov.run() is None
    prov2 = _provider(stores_trio, engine=_FakeEngine(CoachResult([], False)), store=False)
    assert prov2.available is False and await prov2.run() is None


@pytest.mark.asyncio
async def test_inflight_guard_blocks_second_run(stores_trio):
    engine = _FakeEngine(CoachResult(cards=[_CARD], is_fallback=False))
    prov = _provider(stores_trio, engine=engine)
    prov._inflight = True
    assert await prov.run() is None
    assert engine.calls == 0
    prov._inflight = False
    assert (await prov.run())["status"] == "ok"


def test_accessors_fail_soft(stores_trio):
    class _Boom:
        def latest(self): raise RuntimeError("boom")
        def list_runs(self, *, limit=20): raise RuntimeError("boom")
        def get(self, run_id): raise RuntimeError("boom")
    from src.web.coach import CoachProvider
    prov = CoachProvider(store=_Boom())
    assert prov.latest() is None
    assert prov.runs() == []
    assert prov.get("x") is None


def test_nav_visible_logic(stores_trio):
    from src.web.coach import CoachProvider
    _, _, coach_store = stores_trio
    assert CoachProvider(store=coach_store).nav_visible is True
    assert CoachProvider(store=None).nav_visible is False
    assert CoachProvider(store=coach_store, enabled=False).nav_visible is False


@pytest.mark.asyncio
async def test_run_degrades_on_missing_or_unparseable_documents(stores_trio):
    from src.config import AppConfig
    from src.settings.documents import Documents
    from src.web.coach import CoachProvider
    cfg = AppConfig.model_validate({
        "filters": {"titles": ["engineer"], "seniority_allow": ["senior"]},
        "relevance": {"enabled": True, "provider": "ollama", "model": "m"},
    })
    engine = _FakeEngine(CoachResult(cards=[_CARD], is_fallback=False))
    seen, rejected, coach_store = stores_trio
    prov = CoachProvider(store=coach_store, seen=seen, rejected=rejected, engine=engine, cfg=cfg,
                         documents=Documents(profile=None, resume_content="{not json"),
                         provider_name="fake", model_name="fake-1")
    run = await prov.run()
    assert run is not None and run["status"] == "ok"
    snap = engine.last_snapshot
    assert snap.profile is None                  # missing document degraded, didn't block
    assert snap.resume_bank is None              # unparseable content degraded
    assert snap.config["filters"]["titles"] == ["engineer"]
    assert snap.meta["applied_count"] == 1


@pytest.mark.asyncio
async def test_run_snapshot_includes_saved_documents(stores_trio):
    from pathlib import Path
    from src.config import AppConfig
    from src.settings.documents import Documents
    from src.web.coach import CoachProvider
    engine = _FakeEngine(CoachResult(cards=[_CARD], is_fallback=False))
    seen, rejected, coach_store = stores_trio
    prov = CoachProvider(
        store=coach_store, seen=seen, rejected=rejected, engine=engine, cfg=AppConfig(),
        documents=Documents(profile="# my profile",
                            resume_content=Path("resume/content.example.json").read_text()),
    )
    await prov.run()
    assert engine.last_snapshot.profile == "# my profile"
    assert engine.last_snapshot.resume_bank is not None


from tests.auth_helpers import signed_in_client

from src.web.app import create_app
from src.web.repo import TriageRepo
from tests.settings_helpers import configured_stores


@pytest.fixture
def coach_client(tmp_path, monkeypatch):
    monkeypatch.setenv("JOB_AGG_TAILORED_DIR", str(tmp_path / "tailored"))
    conn = connect(":memory:")
    stores = configured_stores(conn)
    seen = stores.seen
    rejected = stores.rejected
    coach_store = stores.coach
    seen.claim_for_notify("greenhouse:acme:1", score=8, posting=_posting("greenhouse:acme:1"))
    seen.set_status("greenhouse:acme:1", "applied")
    from src.web.coach import CoachProvider
    engine = _FakeEngine(CoachResult(cards=[_CARD], is_fallback=False))
    prov = CoachProvider(store=coach_store, seen=seen, rejected=rejected,
                         engine=engine, provider_name="fake", model_name="fake-1")
    app = create_app(repo=TriageRepo(seen), stores=stores, coach=prov)
    return signed_in_client(app), prov, engine


def test_app_coach_follows_settings_without_restart(tmp_path, monkeypatch):
    from src.settings.service import ConfigService
    from tests.settings_helpers import configured_stores
    monkeypatch.setenv("JOB_AGG_TAILORED_DIR", str(tmp_path / "tailored"))
    stores = configured_stores(connect(":memory:"), {"coach": {"enabled": True}})
    service = ConfigService(stores.settings, env={})
    client = signed_in_client(create_app(stores=stores, service=service))
    assert 'href="/coach"' in client.get("/").text
    service.save_settings({"coach": {"enabled": False}}, source="cli")
    assert 'href="/coach"' not in client.get("/").text


def test_app_pages_survive_a_failing_coach_builder(tmp_path, monkeypatch):
    """A broken _build_coach (bad client, missing dependency, ...) must not
    500 every page — coach.enabled defaults to True, so coach_nav_visible
    (called from base.html on nearly every page) would otherwise take down
    the whole app until the settings generation moves."""
    import src.handler
    from tests.settings_helpers import configured_stores
    monkeypatch.setenv("JOB_AGG_TAILORED_DIR", str(tmp_path / "tailored"))

    def _boom(cfg):
        raise RuntimeError("boom")

    monkeypatch.setattr(src.handler, "_build_coach", _boom)
    stores = configured_stores(connect(":memory:"))
    client = signed_in_client(create_app(stores=stores))
    r = client.get("/")
    assert r.status_code == 200
    assert 'href="/coach"' in r.text          # nav still renders: enabled + available
    r2 = client.get("/coach")
    assert r2.status_code == 200
    assert "not configured" in r2.text        # engine is None: can_run is False


def test_coach_page_renders_empty_state(coach_client):
    client, _, _ = coach_client
    r = client.get("/coach")
    assert r.status_code == 200
    assert "No recommendations yet" in r.text
    assert "Generate recommendations" in r.text


def test_coach_nav_link_present(coach_client):
    client, _, _ = coach_client
    assert 'href="/coach"' in client.get("/").text


def test_post_run_returns_cards_fragment_and_persists(coach_client):
    client, prov, engine = coach_client
    r = client.post("/coach/run")
    assert r.status_code == 200
    assert "Widen titles" in r.text
    assert "filters" in r.text and "impact: high" in r.text
    assert engine.calls == 1
    assert prov.latest()["status"] == "ok"
    # page now shows the run + past-runs list
    page = client.get("/coach").text
    assert "Widen titles" in page and "Past runs" in page


def test_post_run_busy_returns_fragment_without_calling_engine(coach_client):
    client, prov, engine = coach_client
    prov._inflight = True
    r = client.post("/coach/run")
    assert r.status_code == 200
    assert "already in progress" in r.text
    assert engine.calls == 0
    prov._inflight = False


def test_past_run_view(coach_client):
    client, prov, _ = coach_client
    client.post("/coach/run")
    run_id = prov.latest()["run_id"]
    r = client.get(f"/coach/runs/{run_id}")
    assert r.status_code == 200 and "Widen titles" in r.text
    assert client.get("/coach/runs/nope").status_code == 404


def test_error_run_renders_failure(coach_client):
    client, prov, engine = coach_client
    engine._result = CoachResult(cards=[], is_fallback=True, error_type="ConnectError")
    client.post("/coach/run")
    assert "Last run failed" in client.get("/coach").text


def test_low_sample_banner(coach_client):
    client, _, _ = coach_client
    client.post("/coach/run")
    assert "Small sample" in client.get("/coach").text  # 1 application < 10


def test_unavailable_store_renders_explainer(tmp_path, monkeypatch):
    monkeypatch.setenv("JOB_AGG_TAILORED_DIR", str(tmp_path / "tailored"))
    conn = connect(":memory:")
    stores = configured_stores(conn)
    seen = stores.seen
    from src.web.coach import CoachProvider
    app = create_app(repo=TriageRepo(seen), stores=stores, coach=CoachProvider(store=None))
    client = signed_in_client(app)
    r = client.get("/coach")
    assert r.status_code == 200 and "unavailable" in r.text
    assert client.post("/coach/run").status_code == 409
    assert 'href="/coach"' not in client.get("/").text  # nav hidden


def test_engineless_provider_explains_not_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("JOB_AGG_TAILORED_DIR", str(tmp_path / "t2"))
    conn = connect(":memory:")
    stores = configured_stores(conn)
    seen = stores.seen
    coach_store = stores.coach
    from src.web.coach import CoachProvider
    app = create_app(repo=TriageRepo(seen), stores=stores,
                     coach=CoachProvider(store=coach_store, seen=seen))
    client = signed_in_client(app)
    r = client.get("/coach")
    assert r.status_code == 200 and "not configured" in r.text
