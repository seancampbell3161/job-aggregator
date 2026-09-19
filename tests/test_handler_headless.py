import logging
from contextlib import asynccontextmanager

import pytest

from src.sqlite_db import connect
from tests.settings_helpers import make_service


def test_headless_is_a_valid_tier():
    from src.handler import _VALID_TIERS
    assert "headless" in _VALID_TIERS


def test_tier_literal_includes_headless():
    import typing
    from src.models import Tier
    assert "headless" in typing.get_args(Tier)


def _headless_boards_doc():
    return {
        "relevance": {"score_low": 4},
        "sources": {
            "avature": [
                {"careers_url": "https://careers.example.com/search", "company": "Example Co"},
            ]
        },
    }


def _phenom_only_doc():
    return {
        "relevance": {"score_low": 4},
        "sources": {
            "phenom": [{"careers_url": "https://careers.example.com/phenom"}],
        },
    }


@pytest.mark.asyncio
async def test_headless_tier_falls_through_for_phenom_only_boards_without_a_browser(
    monkeypatch, caplog,
):
    """Regression: Phenom polls on the "ats" tier via httpx (PhenomConnector.tier
    == "ats") and build_connectors's headless branch builds only from
    cfg.sources.avature — Phenom boards are never driven by a browser. A config
    with Phenom boards and no Avature boards must not trip the
    headless_unavailable skip: this invocation needs no browser at all, so
    skipping it would both log spurious noise every cycle and drop this
    tier's cycle telemetry (record_cycle) for no reason."""
    from src import handler

    monkeypatch.setattr("src.headless.headless_available", lambda: False)
    service = make_service(_phenom_only_doc(), conn=connect(":memory:"))
    with caplog.at_level(logging.WARNING):
        result = await handler._run("headless", service=service, dry_run=True)

    assert "skipped" not in result
    assert result.get("reason") != "headless_unavailable"
    assert "headless_unavailable" not in [r.message for r in caplog.records]


@pytest.mark.asyncio
async def test_headless_tier_skips_with_a_named_log_when_no_browser_is_installed(
    monkeypatch, caplog,
):
    from src import handler

    monkeypatch.setattr("src.headless.headless_available", lambda: False)
    service = make_service(_headless_boards_doc(), conn=connect(":memory:"))
    with caplog.at_level(logging.WARNING):
        result = await handler._run("headless", service=service)

    assert result == {"tier": "headless", "skipped": True, "reason": "headless_unavailable"}
    assert "headless_unavailable" in [r.message for r in caplog.records]


@pytest.mark.asyncio
async def test_headless_tier_runs_normally_when_the_browser_is_installed(monkeypatch):
    """Control: the skip must be caused by the missing browser, not by the
    board configuration — otherwise the test above proves nothing.

    headless_available() is stubbed True, but src.headless.browser_session is
    also stubbed to a fake async context manager: Playwright is not installed
    in this environment, so if _run reached the real browser_session it would
    raise ImportError instead of exercising the branch under test."""
    from src import handler

    @asynccontextmanager
    async def _fake_browser():
        yield object()

    monkeypatch.setattr("src.headless.headless_available", lambda: True)
    monkeypatch.setattr("src.headless.browser_session", _fake_browser)
    service = make_service(_headless_boards_doc(), conn=connect(":memory:"))
    result = await handler._run("headless", service=service, dry_run=True)

    assert result.get("reason") != "headless_unavailable"
