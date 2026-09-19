"""The preview is a dry run: it shows real postings, delivers nothing, and
leaves no state behind."""
import pytest

from src.web.app import create_app
from tests.auth_helpers import signed_in_client
from tests.settings_helpers import WEB_TEST_SETTINGS, make_service

CONFIGURED = {
    **WEB_TEST_SETTINGS,
    "filters": {"titles": ["engineer"], "max_age_days": 2},
    "sources": {"greenhouse": ["acme"]},
}


def _app(tmp_path, monkeypatch):
    monkeypatch.setenv("JOB_AGG_SQLITE_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("JOB_AGG_TAILORED_DIR", str(tmp_path / "tailored"))
    app = create_app(service=make_service(
        CONFIGURED,
        documents={"resume_text": "Ten years of backend engineering.",
                   "profile": "## Quick summary\nBackend engineer."},
        secrets={"ntfy_topic_url": "https://ntfy.sh/some-test-topic"},
    ))
    # Every OTHER wizard step must be complete (or skipped) for the redirect
    # assertion below — /wizard always routes to the first INCOMPLETE step,
    # and "preview" is the last of six. The "llm" step is optional and never
    # completes on its own (relevance.enabled defaults to False), so it must
    # be explicitly skipped, same as tests/web/wizard/test_steps.py does.
    app.state.stores.wizard.skip("llm")
    return app


def test_step_offers_a_start_button(tmp_path, monkeypatch):
    r = signed_in_client(_app(tmp_path, monkeypatch)).get("/wizard/preview")
    assert "/wizard/preview/start" in r.text


# --- Finish must not loop: previewing is the last step, and nothing about
# visiting this page (unlike every other step) requires the user to act on
# it before leaving. Before a preview has actually finished (ok or error),
# preview.complete() is False, so a plain `href="/wizard"` link would just
# route straight back here (next_step() re-picks the first incomplete,
# unskipped step) -- the exact loop this file's docstring exists to catch.
# Finish must instead post a skip, the same escape hatch the companies step
# uses for the identical problem. ---

def test_finish_posts_a_skip_before_a_preview_has_run(tmp_path, monkeypatch):
    r = signed_in_client(_app(tmp_path, monkeypatch)).get("/wizard/preview")
    assert "Finish</button>" in r.text
    assert 'href="/wizard">Finish' not in r.text


def test_finish_links_straight_to_wizard_once_a_preview_has_run(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    app.state.stores.wizard.put("preview", {"status": "ok", "matches": [],
                                            "fetched": 3, "matched": 0})
    r = signed_in_client(app).get("/wizard/preview")
    assert 'href="/wizard">Finish' in r.text
    assert "Finish</button>" not in r.text


def test_finish_button_actually_advances_past_an_unfinished_preview(tmp_path, monkeypatch):
    """Not just that the button LOOKS like a skip form (the test above) --
    posting to it, the way a browser submitting that form would, must
    actually leave the wizard. Every other step is already complete (or
    skipped) by _app(), so once preview is skipped too there is nothing
    left to ask."""
    app = _app(tmp_path, monkeypatch)
    client = signed_in_client(app)
    client.post("/wizard/preview/skip")
    assert client.get("/wizard", follow_redirects=False
                      ).headers["location"] == "/wizard/done"


def test_starting_records_a_running_status(tmp_path, monkeypatch):
    """POST /wizard/preview/start schedules a REAL background task
    (asyncio.create_task) that TestClient's persistent event loop can and
    does go on to actually run — including, unmocked, a genuine HTTP call to
    Greenhouse for the configured "acme" board. run_preview is stubbed here
    so that never happens, however long the loop takes to get to the task.
    (Patching run_preview itself, not run_once several calls deeper, also
    sidesteps a monkeypatch-teardown race: the coroutine object handed to
    asyncio.create_task is already bound to the fake by the time it's
    created, so it stays safe even if teardown reverts the module attribute
    before the task is actually scheduled to run.)"""
    app = _app(tmp_path, monkeypatch)

    async def fake_run_preview(app_):
        return {"status": "ok"}

    monkeypatch.setattr("src.web.wizard.preview.run_preview", fake_run_preview)
    signed_in_client(app).post("/wizard/preview/start")
    assert app.state.stores.wizard.get("preview")["status"] in ("running", "ok", "error")


def test_status_renders_the_matches(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    app.state.stores.wizard.put("preview", {
        "status": "ok",
        "matches": [{"title": "Staff Engineer", "company": "Acme",
                     "location": "Remote (US)", "apply_url": "https://acme.test/1",
                     "score": 8}],
        "fetched": 40, "matched": 1,
    })
    r = signed_in_client(app).get("/wizard/preview/status")
    assert "Staff Engineer" in r.text and "Acme" in r.text


def test_an_empty_result_is_a_warning_not_an_error(tmp_path, monkeypatch):
    """Zero matches means the filters are too narrow — which is exactly what
    this step exists to reveal."""
    app = _app(tmp_path, monkeypatch)
    app.state.stores.wizard.put("preview", {"status": "ok", "matches": [],
                                            "fetched": 40, "matched": 0})
    r = signed_in_client(app).get("/wizard/preview/status")
    assert "/wizard/review" in r.text
    assert "narrow" in r.text.lower()


def test_a_failed_run_is_reported_and_does_not_block(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    app.state.stores.wizard.put("preview", {"status": "error", "error": "timed out"})
    r = signed_in_client(app).get("/wizard/preview/status")
    assert "timed out" in r.text
    assert signed_in_client(app).get("/wizard", follow_redirects=False
                                     ).headers["location"] == "/wizard/done"


def test_running_status_polls_and_a_terminal_status_does_not(tmp_path, monkeypatch):
    """The HTMX contract: a `running` fragment re-polls itself every 2s via
    outerHTML swap; a terminal (ok/error) fragment carries no trigger, so
    polling stops naturally once the run ends — never an endless poll against
    a finished run."""
    app = _app(tmp_path, monkeypatch)
    store = app.state.stores.wizard
    client = signed_in_client(app)

    store.put("preview", {"status": "running"})
    running_html = client.get("/wizard/preview/status").text
    assert 'hx-trigger="every 2s"' in running_html
    assert 'hx-get="/wizard/preview/status"' in running_html
    assert 'hx-swap="outerHTML"' in running_html

    store.put("preview", {"status": "ok", "matches": [], "fetched": 0, "matched": 0})
    done_html = client.get("/wizard/preview/status").text
    assert "hx-trigger" not in done_html


@pytest.mark.asyncio
async def test_run_preview_calls_run_once_read_only(tmp_path, monkeypatch):
    """The guarantee, asserted at the call: dry_run on, ignore_seen on, and
    no sinks — belt and braces, since either alone would suffice."""
    app = _app(tmp_path, monkeypatch)
    captured = {}

    async def fake_run_once(**kwargs):
        captured.update(kwargs)
        from src.orchestrator import RunResult
        return RunResult()

    monkeypatch.setattr("src.web.wizard.preview.run_once", fake_run_once)
    from src.web.wizard.preview import run_preview
    await run_preview(app)
    assert captured["dry_run"] is True
    assert captured["ignore_seen"] is True
    assert list(captured["sinks"]) == []


@pytest.mark.asyncio
async def test_run_preview_caps_the_connector_count(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    captured = {}

    async def fake_run_once(**kwargs):
        captured.update(kwargs)
        from src.orchestrator import RunResult
        return RunResult()

    monkeypatch.setattr("src.web.wizard.preview.run_once", fake_run_once)
    monkeypatch.setattr(
        "src.web.wizard.preview.build_connectors",
        lambda *a, **k: [object() for _ in range(25)],
    )
    from src.web.wizard.preview import MAX_BOARDS, run_preview
    await run_preview(app)
    assert len(captured["connectors"]) == MAX_BOARDS


@pytest.mark.asyncio
async def test_a_timeout_records_an_error_record(tmp_path, monkeypatch):
    """Not just "some error happens": the run must actually be CUT SHORT by
    the timeout, not merely crash later after the full 10s sleep completes
    (e.g. from `result` being None once the mocked run_once's implicit
    None return reaches `.fetched_count`). Elapsed time and the specific
    message both pin that down — a test that only checked status == "error"
    would still pass if asyncio.wait_for's timeout were silently dropped."""
    import asyncio
    import time
    app = _app(tmp_path, monkeypatch)

    async def slow(**kwargs):
        await asyncio.sleep(10)

    monkeypatch.setattr("src.web.wizard.preview.run_once", slow)
    monkeypatch.setattr("src.web.wizard.preview.BUDGET_SECONDS", 0.01)
    from src.web.wizard.preview import run_preview
    started = time.monotonic()
    record = await run_preview(app)
    elapsed = time.monotonic() - started
    assert elapsed < 5, "the run was not actually cut short by the timeout"
    assert record["status"] == "error"
    assert "longer than" in record["error"]
    assert app.state.stores.wizard.get("preview")["status"] == "error"


@pytest.mark.asyncio
async def test_a_crash_records_an_error_rather_than_raising(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)

    async def boom(**kwargs):
        raise RuntimeError("connector exploded")

    monkeypatch.setattr("src.web.wizard.preview.run_once", boom)
    from src.web.wizard.preview import run_preview
    assert (await run_preview(app))["status"] == "error"
