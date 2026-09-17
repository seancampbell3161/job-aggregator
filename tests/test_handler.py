from pathlib import Path

import httpx
import pytest

from tests.settings_helpers import make_service, seed_settings

_MINIMAL_SETTINGS = """
filters:
  titles: ["software engineer"]
  seniority_allow: ["mid", "senior"]
  location: {remote_must_be_us: true, allowed_cities: [], allow_unknown: true}
  comp_floor_usd: 120000
  stack_any_of: ["python"]
quiet_hours: {timezone: "UTC", start: "23:00", end: "07:00"}
sources:
  greenhouse: []
  lever: []
  ashby: []
  workable: []
  hn_who_is_hiring: {enabled: false}
schedules: {ats_minutes: 2, slow_minutes: 15}
"""


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("JOB_AGG_NTFY_TOPIC_URL", "https://ntfy.test/x")
    monkeypatch.setenv("JOB_AGG_DISCORD_WEBHOOK_URL", "https://discord.test/x")
    # Minimal settings with no sources → the handler produces a zero-result run.
    seed_settings(_MINIMAL_SETTINGS)


def test_run_returns_zero_result_summary_for_empty_sources():
    import asyncio
    from src.handler import _run
    result = asyncio.run(_run(tier="ats"))
    assert result["fetched_count"] == 0
    assert result["matched_count"] == 0
    assert result["tier"] == "ats"


from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_handler_routes_discovery_tier_to_discovery_routine(tmp_path, monkeypatch):
    """tier=discovery invokes run_discovery instead of run_once. board_discovery
    is explicitly disabled: it's tested on its own in
    test_discovery_tier_runs_board_sweep, and this test only mocks
    run_discovery/recover_suppressed, not the real (network-bound) board
    sweep — leaving board_discovery_enabled at its default (True) would make
    it sweep real seed-file domains over the network."""
    seed_settings(
        """
filters:
  titles: ["software engineer"]
  seniority_allow: ["mid", "senior"]
  location:
    remote_must_be_us: true
    allowed_cities: []
    allow_unknown: true
  comp_floor_usd: 120000
  stack_any_of: ["python"]
quiet_hours:
  timezone: "UTC"
  start: "23:00"
  end: "07:00"
sources:
  greenhouse: []
discovery:
  enabled: true
  max_validations_per_run: 10
  quarantine_after_failures: 5
  board_discovery_enabled: false
schedules:
  ats_minutes: 1
  slow_minutes: 15
  discovery_hours: 24
        """
    )
    monkeypatch.setenv("JOB_AGG_NTFY_TOPIC_URL", "x")
    monkeypatch.setenv("JOB_AGG_DISCORD_WEBHOOK_URL", "y")

    from src.handler import _run

    from unittest.mock import AsyncMock, patch
    with patch("src.handler.run_discovery", new=AsyncMock()) as mock_disc, \
         patch("src.handler.recover_suppressed", new=AsyncMock()) as mock_rec:
        result = await _run(tier="discovery")

    mock_disc.assert_called_once()
    mock_rec.assert_called_once()
    assert result["tier"] == "discovery"


def _minimal_app_config(*, relevance_kwargs=None, secrets_kwargs=None):
    """Builds a minimal AppConfig for relevance-scorer tests."""
    from datetime import time
    from zoneinfo import ZoneInfo

    from src.config import (
        AppConfig, FiltersConfig, LocationFilterConfig, QuietHoursConfig,
        RelevanceConfig, SchedulesConfig, Secrets, SourcesConfig,
    )

    return AppConfig(
        filters=FiltersConfig(
            titles=["software engineer"], seniority_allow=["senior"],
            location=LocationFilterConfig(remote_must_be_us=True, allowed_cities=[], allow_unknown=True),
            comp_floor_usd=120000, stack_any_of=["python"],
        ),
        quiet_hours=QuietHoursConfig(timezone=ZoneInfo("UTC"), start=time(23, 0), end=time(7, 0)),
        sources=SourcesConfig(),
        schedules=SchedulesConfig(ats_minutes=1, slow_minutes=15),
        secrets=Secrets(ntfy_topic_url="x", discord_webhook_url="y", **(secrets_kwargs or {})),
        relevance=RelevanceConfig(**(relevance_kwargs or {})),
    )


def test_build_relevance_scorer_returns_none_when_disabled():
    from src.handler import _build_relevance_scorer
    cfg = _minimal_app_config(relevance_kwargs={"enabled": False})
    assert _build_relevance_scorer(cfg, "# profile") is None


def test_build_relevance_scorer_returns_none_when_api_key_missing():
    from src.handler import _build_relevance_scorer
    cfg = _minimal_app_config(
        relevance_kwargs={"enabled": True},
        secrets_kwargs={"anthropic_api_key": ""},
    )
    assert _build_relevance_scorer(cfg, "# profile") is None


def test_build_relevance_scorer_returns_none_when_profile_missing():
    """relevance enabled + key set + no profile document → soft-fail to None,
    don't crash the whole pipeline run."""
    from src.handler import _build_relevance_scorer

    cfg = _minimal_app_config(
        relevance_kwargs={"enabled": True},
        secrets_kwargs={"anthropic_api_key": "sk-ant-test"},
    )
    # Should not raise — soft-fails to None, the cycle continues unscored.
    assert _build_relevance_scorer(cfg, None) is None


def test_build_relevance_scorer_returns_anthropic_scorer_by_default():
    """provider unspecified → AnthropicRelevanceScorer (the current default)."""
    from src.handler import _build_relevance_scorer
    from src.relevance import RelevanceScorer

    cfg = _minimal_app_config(
        relevance_kwargs={"enabled": True},
        secrets_kwargs={"anthropic_api_key": "sk-ant-test"},
    )
    scorer = _build_relevance_scorer(cfg, "# profile")
    assert isinstance(scorer, RelevanceScorer)


def test_build_relevance_scorer_returns_gemini_scorer_when_provider_gemini():
    """provider='gemini' + google_api_key set → GeminiRelevanceScorer."""
    from src.handler import _build_relevance_scorer
    from src.relevance import GeminiRelevanceScorer

    cfg = _minimal_app_config(
        relevance_kwargs={
            "enabled": True,
            "provider": "gemini",
            "model": "gemini-2.0-flash",
        },
        secrets_kwargs={"google_api_key": "g-test-123"},
    )
    scorer = _build_relevance_scorer(cfg, "# profile")
    assert isinstance(scorer, GeminiRelevanceScorer)


def test_build_relevance_scorer_returns_none_when_provider_gemini_but_key_missing():
    """provider='gemini' + empty google_api_key → None (soft-disable, log warning)."""
    from src.handler import _build_relevance_scorer

    cfg = _minimal_app_config(
        relevance_kwargs={"enabled": True, "provider": "gemini"},
        secrets_kwargs={"google_api_key": ""},  # missing
    )
    assert _build_relevance_scorer(cfg, "# profile") is None


def test_build_relevance_scorer_returns_ollama_scorer_when_provider_ollama():
    """provider='ollama' + ollama_api_key set → OllamaRelevanceScorer."""
    from src.handler import _build_relevance_scorer
    from src.relevance import OllamaRelevanceScorer

    cfg = _minimal_app_config(
        relevance_kwargs={
            "enabled": True,
            "provider": "ollama",
            "model": "gpt-oss:20b",
            "ollama_host": "https://ollama.com",
        },
        secrets_kwargs={"ollama_api_key": "ol-test-123"},
    )
    scorer = _build_relevance_scorer(cfg, "# profile")
    assert isinstance(scorer, OllamaRelevanceScorer)


def test_build_relevance_scorer_returns_none_when_provider_ollama_but_key_missing():
    """provider='ollama' + empty ollama_api_key + cloud host → None (soft-disable)."""
    from src.handler import _build_relevance_scorer

    cfg = _minimal_app_config(
        relevance_kwargs={"enabled": True, "provider": "ollama", "ollama_host": "https://ollama.com"},
        secrets_kwargs={"ollama_api_key": ""},  # missing
    )
    assert _build_relevance_scorer(cfg, "# profile") is None


def _minimal_app_config_with_gaps(*, gap_kwargs=None, secrets_kwargs=None, relevance_kwargs=None):
    from datetime import time
    from zoneinfo import ZoneInfo
    from src.config import (
        AppConfig, FiltersConfig, GapAnalysisConfig, LocationFilterConfig,
        QuietHoursConfig, RelevanceConfig, SchedulesConfig, Secrets, SourcesConfig,
    )
    return AppConfig(
        filters=FiltersConfig(
            titles=["software engineer"], seniority_allow=["senior"],
            location=LocationFilterConfig(remote_must_be_us=True, allowed_cities=[], allow_unknown=True),
            comp_floor_usd=120000, stack_any_of=["python"],
        ),
        quiet_hours=QuietHoursConfig(timezone=ZoneInfo("UTC"), start=time(23, 0), end=time(7, 0)),
        sources=SourcesConfig(),
        schedules=SchedulesConfig(ats_minutes=1, slow_minutes=15),
        secrets=Secrets(ntfy_topic_url="x", discord_webhook_url="y", **(secrets_kwargs or {})),
        relevance=RelevanceConfig(**(relevance_kwargs or {})),
        gap_analysis=GapAnalysisConfig(**(gap_kwargs or {})),
    )


def test_build_gap_analyzer_none_when_disabled():
    from src.handler import _build_gap_analyzer
    cfg = _minimal_app_config_with_gaps(gap_kwargs={"enabled": False})
    assert _build_gap_analyzer(cfg, "# resume") is None


def test_build_gap_analyzer_none_when_resume_missing():
    from src.handler import _build_gap_analyzer
    cfg = _minimal_app_config_with_gaps(
        gap_kwargs={"enabled": True},
        secrets_kwargs={"anthropic_api_key": "sk-ant-test"},
    )
    assert _build_gap_analyzer(cfg, None) is None


def test_build_gap_analyzer_none_when_key_missing():
    from src.handler import _build_gap_analyzer
    cfg = _minimal_app_config_with_gaps(
        gap_kwargs={"enabled": True},
        secrets_kwargs={"anthropic_api_key": ""},
    )
    assert _build_gap_analyzer(cfg, "# resume") is None


def test_build_gap_analyzer_anthropic_by_default():
    from src.handler import _build_gap_analyzer
    from src.gaps import AnthropicGapAnalyzer
    cfg = _minimal_app_config_with_gaps(
        gap_kwargs={"enabled": True},
        relevance_kwargs={"provider": "anthropic"},
        secrets_kwargs={"anthropic_api_key": "sk-ant-test"},
    )
    assert isinstance(_build_gap_analyzer(cfg, "# resume"), AnthropicGapAnalyzer)


def test_build_gap_analyzer_uses_own_provider_over_relevance():
    """gap_analysis.provider overrides relevance.provider when set."""
    from src.handler import _build_gap_analyzer
    from src.gaps import GeminiGapAnalyzer
    cfg = _minimal_app_config_with_gaps(
        gap_kwargs={"enabled": True, "provider": "gemini"},
        relevance_kwargs={"provider": "anthropic"},
        secrets_kwargs={"google_api_key": "g-test"},
    )
    assert isinstance(_build_gap_analyzer(cfg, "# resume"), GeminiGapAnalyzer)


@pytest.mark.asyncio
async def test_run_digest_skipped_when_disabled(monkeypatch):
    seed_settings(
        """
filters:
  titles: ["software engineer"]
  seniority_allow: ["senior"]
  location: {remote_must_be_us: true, allowed_cities: [], allow_unknown: true}
  comp_floor_usd: 120000
  stack_any_of: ["python"]
quiet_hours: {timezone: "UTC", start: "23:00", end: "07:00"}
sources: {greenhouse: []}
schedules: {ats_minutes: 1, slow_minutes: 15}
"""
    )
    monkeypatch.setenv("JOB_AGG_NTFY_TOPIC_URL", "x")
    monkeypatch.setenv("JOB_AGG_DISCORD_WEBHOOK_URL", "y")
    from src.handler import _run
    result = await _run(tier="digest")
    assert result["tier"] == "digest"
    assert result["skipped"] is True


@pytest.mark.asyncio
async def test_run_digest_posts_when_enabled(monkeypatch):
    seed_settings(
        """
filters:
  titles: ["software engineer"]
  seniority_allow: ["senior"]
  location: {remote_must_be_us: true, allowed_cities: [], allow_unknown: true}
  comp_floor_usd: 120000
  stack_any_of: ["python"]
quiet_hours: {timezone: "UTC", start: "23:00", end: "07:00"}
sources: {greenhouse: []}
schedules: {ats_minutes: 1, slow_minutes: 15}
gap_analysis: {enabled: true}
"""
    )
    monkeypatch.setenv("JOB_AGG_NTFY_TOPIC_URL", "x")
    monkeypatch.setenv("JOB_AGG_DISCORD_WEBHOOK_URL", "https://discord.test/wh")

    from src.stores import build_stores
    s = build_stores().seen
    s.claim_for_notify("g:1", gaps=["Kubernetes", "Kafka"])
    s.claim_for_notify("g:2", gaps=["Kubernetes"])

    sent: dict = {}

    async def fake_send(client, webhook_url, content):
        sent["content"] = content

    from unittest.mock import patch
    from src.handler import _run
    with patch("src.handler.send_gap_digest", new=fake_send):
        result = await _run(tier="digest")

    assert result["tier"] == "digest"
    assert result["matches"] == 2
    assert "Kubernetes (2)" in sent["content"]


@pytest.mark.asyncio
async def test_gaps_report_returns_tally(monkeypatch):
    seed_settings(
        """
filters:
  titles: ["software engineer"]
  seniority_allow: ["senior"]
  location: {remote_must_be_us: true, allowed_cities: [], allow_unknown: true}
  comp_floor_usd: 120000
  stack_any_of: ["python"]
quiet_hours: {timezone: "UTC", start: "23:00", end: "07:00"}
sources: {greenhouse: []}
schedules: {ats_minutes: 1, slow_minutes: 15}
gap_analysis: {enabled: true, digest_window_days: 30}
"""
    )
    monkeypatch.setenv("JOB_AGG_NTFY_TOPIC_URL", "x")
    monkeypatch.setenv("JOB_AGG_DISCORD_WEBHOOK_URL", "y")

    from src.stores import build_stores
    s = build_stores().seen
    s.claim_for_notify("g:1", gaps=["Kubernetes"])
    s.claim_for_notify("g:2", gaps=["Kubernetes"])

    from src.handler import _gaps_report
    result = await _gaps_report(days=None)

    assert result["matches"] == 2
    assert result["gaps"] == [("Kubernetes", 2)]
    assert result["enabled"] is True


def test_run_uses_sqlite_backend(monkeypatch, tmp_path):
    db_path = tmp_path / "t.db"
    monkeypatch.setenv("JOB_AGG_SQLITE_PATH", str(db_path))
    seed_settings(Path("config.example.yaml").read_text())
    monkeypatch.setenv("JOB_AGG_NTFY_TOPIC_URL", "https://ntfy.sh/x")
    monkeypatch.setenv("JOB_AGG_DISCORD_WEBHOOK_URL", "https://discord.test/x")
    # gap_analysis defaults to disabled (GapAnalysisConfig.enabled=False) and
    # config.example.yaml omits it -> digest tier returns the skip dict
    # offline, after _run() has built the SQLite stores.
    import asyncio
    from src import handler
    result = asyncio.run(handler._run(tier="digest"))
    assert result["tier"] == "digest"
    assert result.get("skipped") is True
    # build_stores(sqlite) created the DB file -> proves the backend wiring
    assert db_path.exists()


def test_ollama_local_builds_without_api_key(monkeypatch):
    monkeypatch.delenv("JOB_AGG_OLLAMA_API_KEY", raising=False)
    from src import handler
    cfg = _minimal_app_config(
        relevance_kwargs={
            "enabled": True, "provider": "ollama", "ollama_host": "http://ollama:11434",
        },
    )
    scorer = handler._build_relevance_scorer(cfg, "# profile")
    assert scorer is not None  # built despite no API key (local host)


def test_ollama_cloud_still_requires_key(monkeypatch):
    monkeypatch.delenv("JOB_AGG_OLLAMA_API_KEY", raising=False)
    from src import handler
    cfg = _minimal_app_config(
        relevance_kwargs={
            "enabled": True, "provider": "ollama", "ollama_host": "https://ollama.com",
        },
    )
    assert handler._build_relevance_scorer(cfg, "# profile") is None  # no key + cloud host -> disabled


def test_ollama_local_gap_analyzer_builds_without_api_key(monkeypatch):
    """Local Ollama host + no API key → gap analyzer builds successfully (symmetric with relevance scorer test)."""
    monkeypatch.delenv("JOB_AGG_OLLAMA_API_KEY", raising=False)
    from src import handler
    from src.gaps import OllamaGapAnalyzer
    cfg = _minimal_app_config_with_gaps(
        gap_kwargs={"enabled": True, "provider": "ollama"},
        relevance_kwargs={"ollama_host": "http://ollama:11434"},
    )
    analyzer = handler._build_gap_analyzer(cfg, "# resume")
    assert analyzer is not None  # built despite no API key (local host)
    assert isinstance(analyzer, OllamaGapAnalyzer)


@pytest.mark.asyncio
async def test_ping_heartbeat_hits_url_and_swallows_errors():
    from src.handler import _ping_heartbeat
    calls = []
    def ok(request):
        calls.append(str(request.url))
        return httpx.Response(200)
    await _ping_heartbeat(
        "https://hc.test/ping/1",
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(ok)),
    )
    assert calls == ["https://hc.test/ping/1"]

    def boom(request):
        raise httpx.ConnectError("down")
    # must not raise
    await _ping_heartbeat(
        "https://hc.test/ping/1",
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(boom)),
    )


def _minimal_cfg_yaml() -> str:
    return (
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


def _sqlite_env(monkeypatch, tmp_path):
    monkeypatch.setenv("JOB_AGG_NTFY_TOPIC_URL", "https://ntfy.test/x")
    monkeypatch.setenv("JOB_AGG_DISCORD_WEBHOOK_URL", "https://discord.test/x")
    monkeypatch.setenv("JOB_AGG_SQLITE_PATH", str(tmp_path / "t.db"))
    monkeypatch.delenv("JOB_AGG_OPS_NTFY_TOPIC_URL", raising=False)
    monkeypatch.delenv("JOB_AGG_OPS_DISCORD_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("JOB_AGG_HEARTBEAT_URL", raising=False)
    seed_settings(_minimal_cfg_yaml())


@pytest.mark.asyncio
async def test_run_wires_rejected_store_and_records_new_count(tmp_path, monkeypatch):
    from src.handler import _run
    from src.orchestrator import RunResult
    _sqlite_env(monkeypatch, tmp_path)
    captured: dict = {}

    async def fake_run_once(**kwargs):
        captured.update(kwargs)
        return RunResult(fetched_count=5, new_count=4, matched_count=1,
                         notified_count=0, duration_ms=10)

    monkeypatch.setattr("src.handler.run_once", fake_run_once)
    await _run(tier="ats")
    from src.state_sqlite import SqliteRejectedPostingsStore
    assert isinstance(captured["rejected_store"], SqliteRejectedPostingsStore)
    from src.stores import build_stores
    rows = build_stores().events.recent_cycles(1)
    assert rows[-1]["new_count"] == 4


@pytest.mark.asyncio
async def test_run_pings_heartbeat_on_ats_only(tmp_path, monkeypatch):
    from src.handler import _run
    from src.orchestrator import RunResult
    _sqlite_env(monkeypatch, tmp_path)
    monkeypatch.setenv("JOB_AGG_HEARTBEAT_URL", "https://hc.test/ping/1")
    pings: list[str] = []

    async def fake_run_once(**kwargs):
        return RunResult(duration_ms=1)

    async def fake_ping(url, **kwargs):
        pings.append(url)

    monkeypatch.setattr("src.handler.run_once", fake_run_once)
    monkeypatch.setattr("src.handler._ping_heartbeat", fake_ping)
    await _run(tier="ats")
    assert pings == ["https://hc.test/ping/1"]
    await _run(tier="slow")
    assert pings == ["https://hc.test/ping/1"]  # slow tier does not ping
    await _run(tier="ats", dry_run=True)
    assert pings == ["https://hc.test/ping/1"]  # dry-run does not ping


@pytest.mark.asyncio
async def test_run_sends_ops_alert_after_consecutive_degraded_cycles(tmp_path, monkeypatch):
    from src.handler import _run
    from src.orchestrator import RunResult
    _sqlite_env(monkeypatch, tmp_path)
    monkeypatch.setenv("JOB_AGG_OPS_NTFY_TOPIC_URL", "https://ntfy.test/ops")
    sent: list = []

    async def fake_send(client, alert, **kwargs):
        sent.append((alert.condition, kwargs))
        return True

    async def fake_run_once(**kwargs):
        r = RunResult(fetched_count=1, new_count=1, matched_count=1,
                      notified_count=0, duration_ms=1)
        r.llm_failures.append({"stage": "relevance", "error_type": "ConnectError"})
        return r

    monkeypatch.setattr("src.handler.send_ops_alert", fake_send)
    monkeypatch.setattr("src.handler.run_once", fake_run_once)
    await _run(tier="ats")
    assert sent == []  # one degraded cycle — below the K=2 threshold
    await _run(tier="ats")
    assert [c for c, _ in sent] == ["llm_degraded"]
    assert sent[0][1]["ntfy_topic_url"] == "https://ntfy.test/ops"


@pytest.mark.asyncio
async def test_run_rearms_alert_when_send_fails(tmp_path, monkeypatch):
    from src.handler import _run
    from src.orchestrator import RunResult
    _sqlite_env(monkeypatch, tmp_path)
    monkeypatch.setenv("JOB_AGG_OPS_NTFY_TOPIC_URL", "https://ntfy.test/ops")
    sent: list = []

    async def fake_send(client, alert, **kwargs):
        sent.append(alert.condition)
        return False  # every sink failed

    async def fake_run_once(**kwargs):
        r = RunResult(fetched_count=1, new_count=1, matched_count=1,
                      notified_count=0, duration_ms=1)
        r.llm_failures.append({"stage": "relevance", "error_type": "ConnectError"})
        return r

    monkeypatch.setattr("src.handler.send_ops_alert", fake_send)
    monkeypatch.setattr("src.handler.run_once", fake_run_once)
    await _run(tier="ats")
    assert sent == []  # one degraded cycle — below the K=2 threshold
    await _run(tier="ats")
    assert sent == ["llm_degraded"]  # send attempted, but the sink failed
    # The condition re-armed on failure, so the very next degraded cycle
    # retries immediately instead of waiting out the 6h cooldown.
    await _run(tier="ats")
    assert sent == ["llm_degraded", "llm_degraded"]


@pytest.mark.asyncio
async def test_run_skips_ops_alerts_without_ops_sinks(tmp_path, monkeypatch):
    from src.handler import _run
    from src.orchestrator import RunResult
    _sqlite_env(monkeypatch, tmp_path)  # deletes the ops env vars

    async def fake_send(client, alert, **kwargs):
        raise AssertionError("must not send without ops sinks configured")

    async def fake_run_once(**kwargs):
        r = RunResult(duration_ms=1, new_count=1)
        r.llm_failures.append({"stage": "relevance", "error_type": "E"})
        return r

    monkeypatch.setattr("src.handler.send_ops_alert", fake_send)
    monkeypatch.setattr("src.handler.run_once", fake_run_once)
    await _run(tier="ats")
    await _run(tier="ats")  # would fire llm_degraded if sinks were configured


@pytest.mark.asyncio
async def test_discovery_tier_runs_board_sweep(tmp_path, monkeypatch):
    """tier=discovery invokes run_board_discovery with stores.boards when
    board_discovery_enabled and the boards store is present (unlike
    test_handler_routes_discovery_tier_to_discovery_routine, whose config
    disables board_discovery_enabled so the sweep stays inert)."""
    monkeypatch.setenv("JOB_AGG_NTFY_TOPIC_URL", "x")
    monkeypatch.setenv("JOB_AGG_DISCORD_WEBHOOK_URL", "y")
    monkeypatch.setenv("JOB_AGG_SQLITE_PATH", str(tmp_path / "t.db"))
    seed_settings(
        """
filters:
  titles: ["software engineer"]
  seniority_allow: ["mid", "senior"]
  location:
    remote_must_be_us: true
    allowed_cities: []
    allow_unknown: true
  comp_floor_usd: 120000
  stack_any_of: ["python"]
quiet_hours:
  timezone: "UTC"
  start: "23:00"
  end: "07:00"
sources:
  greenhouse: []
discovery:
  enabled: true
  max_validations_per_run: 10
  quarantine_after_failures: 5
  board_discovery_enabled: true
schedules:
  ats_minutes: 1
  slow_minutes: 15
  discovery_hours: 24
        """
    )

    from src.handler import _run
    from src.state_sqlite import SqliteDiscoveredBoardsStore

    called: dict = {}

    async def fake_board(**kwargs):
        called["kwargs"] = kwargs

    with patch("src.handler.run_discovery", new=AsyncMock()), \
         patch("src.handler.recover_suppressed", new=AsyncMock()), \
         patch("src.handler.run_board_discovery", new=fake_board), \
         patch("src.handler.load_seeds", return_value=["fake-seed"]):
        result = await _run(tier="discovery")

    assert called.get("kwargs") is not None  # the sweep was invoked
    assert isinstance(called["kwargs"]["boards"], SqliteDiscoveredBoardsStore)
    assert called["kwargs"]["seeds"] == ["fake-seed"]
    assert result["tier"] == "discovery"
    # The sweep must use its OWN follow_redirects client — locate_careers_urls
    # needs post-redirect final URLs; the shared discovery client (no
    # redirects) would make the sweep discover nothing.
    assert called["kwargs"]["client"].follow_redirects is True


def _board_sweep_cfg_yaml() -> str:
    return """
filters:
  titles: ["software engineer"]
  seniority_allow: ["mid", "senior"]
  location:
    remote_must_be_us: true
    allowed_cities: []
    allow_unknown: true
  comp_floor_usd: 120000
  stack_any_of: ["python"]
quiet_hours:
  timezone: "UTC"
  start: "23:00"
  end: "07:00"
sources:
  greenhouse: []
discovery:
  enabled: true
  max_validations_per_run: 10
  quarantine_after_failures: 5
  board_discovery_enabled: true
schedules:
  ats_minutes: 1
  slow_minutes: 15
  discovery_hours: 24
"""


@pytest.mark.asyncio
async def test_discovery_tier_board_sweep_failure_does_not_skip_recovery(tmp_path, monkeypatch):
    """A raising board sweep (e.g. load_seeds's FileNotFoundError when the seed
    CSV isn't shipped in the image) must not prevent recover_suppressed from
    running — regression test for the fail-soft wrap around the sweep."""
    monkeypatch.setenv("JOB_AGG_NTFY_TOPIC_URL", "x")
    monkeypatch.setenv("JOB_AGG_DISCORD_WEBHOOK_URL", "y")
    monkeypatch.setenv("JOB_AGG_SQLITE_PATH", str(tmp_path / "t.db"))
    seed_settings(_board_sweep_cfg_yaml())

    from src.handler import _run

    with patch("src.handler.run_discovery", new=AsyncMock()), \
         patch("src.handler.recover_suppressed", new=AsyncMock()) as mock_rec, \
         patch("src.handler.load_seeds", side_effect=FileNotFoundError("no seed csv")):
        result = await _run(tier="discovery")

    mock_rec.assert_called_once()
    assert result["tier"] == "discovery"


@pytest.mark.asyncio
async def test_slow_tier_stages_hiringcafe_candidates(tmp_path, monkeypatch):
    """Prod-shaped smoke (PR #24 lesson): the real _run('slow') path — real
    build_connectors, run_once, and sighting drain — with only the
    hiring.cafe HTTP search stubbed at the client seam."""
    monkeypatch.setenv("JOB_AGG_SQLITE_PATH", str(tmp_path / "t.db"))
    seed_settings("""
filters:
  titles: ["zzz-no-title-matches-this"]
  seniority_allow: ["mid", "senior"]
  location: {remote_must_be_us: true, allowed_cities: [], allow_unknown: true}
  comp_floor_usd: 0
  stack_any_of: ["python"]
quiet_hours: {timezone: "UTC", start: "23:00", end: "07:00"}
sources:
  greenhouse: []
  lever: []
  ashby: []
  workable: []
  hn_who_is_hiring: {enabled: false}
  remotive: {enabled: false}
  remoteok: {enabled: false}
  hiringcafe: {enabled: true}
schedules: {ats_minutes: 10, slow_minutes: 15}
discovery: {enabled: true}
""")

    payload = {"pageProps": {"ssrHits": [
        {
            "id": "hc-1", "source": "Greenhouse", "board_token": "newco",
            "apply_url": "https://boards.greenhouse.io/newco/jobs/1",
            "job_information": {"title": "Software Engineer", "description": "d"},
            "enriched_company_data": {"name": "NewCo"},
        },
        {
            "id": "hc-2", "source": "Workday", "board_token": "acme",
            "apply_url": "https://acme.wd5.myworkdayjobs.com/en-US/Ext/job/x/2",
            "job_information": {"title": "Software Engineer", "description": "d"},
            "enriched_company_data": {"name": "Acme"},
        },
    ]}}
    from unittest.mock import AsyncMock
    monkeypatch.setattr("src.hiringcafe.HiringCafeClient.search",
                        AsyncMock(return_value=payload))

    from src.handler import _run
    await _run("slow")

    from src.sqlite_db import connect
    from src.state_sqlite import SqliteDiscoveredBoardsStore, SqliteDiscoveredSlugsStore
    conn = connect(str(tmp_path / "t.db"))
    slugs = SqliteDiscoveredSlugsStore(conn)
    boards = SqliteDiscoveredBoardsStore(conn)
    assert slugs.get("greenhouse:newco").validation_status == "candidate"
    assert slugs.get("greenhouse:newco").origin == "hiringcafe"
    assert boards.get("acme.wd5.myworkdayjobs.com").status == "candidate"


@pytest.mark.asyncio
async def test_slow_tier_mining_disabled_stages_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("JOB_AGG_SQLITE_PATH", str(tmp_path / "t.db"))
    seed_settings("""
filters:
  titles: ["zzz"]
  seniority_allow: ["mid", "senior"]
  location: {remote_must_be_us: true, allowed_cities: [], allow_unknown: true}
  comp_floor_usd: 0
  stack_any_of: ["python"]
quiet_hours: {timezone: "UTC", start: "23:00", end: "07:00"}
sources:
  greenhouse: []
  lever: []
  ashby: []
  workable: []
  hn_who_is_hiring: {enabled: false}
  remotive: {enabled: false}
  remoteok: {enabled: false}
  hiringcafe: {enabled: true}
schedules: {ats_minutes: 10, slow_minutes: 15}
discovery: {enabled: true, hiringcafe_mining_enabled: false}
""")
    from unittest.mock import AsyncMock
    monkeypatch.setattr("src.hiringcafe.HiringCafeClient.search",
                        AsyncMock(return_value={"pageProps": {"ssrHits": []}}))
    from src.handler import _run
    await _run("slow")
    from src.sqlite_db import connect
    from src.state_sqlite import SqliteDiscoveredSlugsStore
    assert SqliteDiscoveredSlugsStore(connect(str(tmp_path / "t.db"))).list_candidates() == []


@pytest.mark.asyncio
async def test_discovery_tier_board_sweep_exception_does_not_skip_recovery(tmp_path, monkeypatch):
    """Same guarantee when run_board_discovery itself raises (not just
    load_seeds) — the whole sweep block is fail-soft."""
    monkeypatch.setenv("JOB_AGG_NTFY_TOPIC_URL", "x")
    monkeypatch.setenv("JOB_AGG_DISCORD_WEBHOOK_URL", "y")
    monkeypatch.setenv("JOB_AGG_SQLITE_PATH", str(tmp_path / "t.db"))
    seed_settings(_board_sweep_cfg_yaml())

    from src.handler import _run

    async def fake_board(**kwargs):
        raise RuntimeError("sweep blew up")

    with patch("src.handler.run_discovery", new=AsyncMock()), \
         patch("src.handler.recover_suppressed", new=AsyncMock()) as mock_rec, \
         patch("src.handler.run_board_discovery", new=fake_board), \
         patch("src.handler.load_seeds", return_value=["fake-seed"]):
        result = await _run(tier="discovery")

    mock_rec.assert_called_once()
    assert result["tier"] == "discovery"


@pytest.mark.asyncio
async def test_discovery_active_set_includes_rippling_config_slugs(tmp_path, monkeypatch):
    """Rider (conversion-chain spec): the discovery-tier active_set build
    enumerates all 6 slug families — a config-listed rippling slug must not
    be re-probed by discovery. board_discovery is explicitly disabled (see
    test_handler_routes_discovery_tier_to_discovery_routine's docstring for
    why: stores.boards is a real SqliteDiscoveredBoardsStore here, and this test doesn't
    mock run_board_discovery)."""
    seed_settings(
        """
filters:
  titles: ["software engineer"]
  seniority_allow: ["mid", "senior"]
  location:
    remote_must_be_us: true
    allowed_cities: []
    allow_unknown: true
  comp_floor_usd: 120000
  stack_any_of: ["python"]
quiet_hours:
  timezone: "UTC"
  start: "23:00"
  end: "07:00"
sources:
  greenhouse: ["ghco"]
  rippling: ["ripco"]
discovery:
  enabled: true
  max_validations_per_run: 10
  quarantine_after_failures: 5
  board_discovery_enabled: false
schedules:
  ats_minutes: 1
  slow_minutes: 15
  discovery_hours: 24
        """
    )
    monkeypatch.setenv("JOB_AGG_NTFY_TOPIC_URL", "x")
    monkeypatch.setenv("JOB_AGG_DISCORD_WEBHOOK_URL", "y")

    from src.handler import _run
    from unittest.mock import AsyncMock, patch
    with patch("src.handler.run_discovery", new=AsyncMock()) as mock_disc, \
         patch("src.handler.recover_suppressed", new=AsyncMock()):
        await _run(tier="discovery")

    active_set = mock_disc.call_args.kwargs["active_set"]
    assert ("rippling", "ripco") in active_set
    assert ("greenhouse", "ghco") in active_set


_VC_CFG_YAML = """
filters:
  titles: ["software engineer"]
  seniority_allow: ["mid", "senior"]
  location:
    remote_must_be_us: true
    allowed_cities: []
    allow_unknown: true
  comp_floor_usd: 120000
  stack_any_of: ["python"]
quiet_hours:
  timezone: "UTC"
  start: "23:00"
  end: "07:00"
sources:
  greenhouse: []
discovery:
  enabled: true
  board_discovery_enabled: false
  vc_firms: ["a16z", "sequoia"]
schedules:
  ats_minutes: 1
  slow_minutes: 15
  discovery_hours: 24
"""


def _vc_handler_env(tmp_path, monkeypatch, yaml_text):
    monkeypatch.setenv("JOB_AGG_NTFY_TOPIC_URL", "x")
    monkeypatch.setenv("JOB_AGG_DISCORD_WEBHOOK_URL", "y")
    monkeypatch.setenv("JOB_AGG_SQLITE_PATH", str(tmp_path / "t.db"))
    seed_settings(yaml_text)


@pytest.mark.asyncio
async def test_discovery_tier_runs_vc_discovery_when_firms_configured(tmp_path, monkeypatch):
    _vc_handler_env(tmp_path, monkeypatch, _VC_CFG_YAML)
    from src.handler import _run

    called: dict = {}

    async def fake_vc(**kwargs):
        called["kwargs"] = kwargs

    with patch("src.handler.run_discovery", new=AsyncMock()), \
         patch("src.handler.recover_suppressed", new=AsyncMock()), \
         patch("src.handler.run_vc_discovery", new=fake_vc):
        result = await _run(tier="discovery")

    kw = called["kwargs"]
    assert kw["firms"] == ["a16z", "sequoia"]
    assert kw["refresh_days"] == 7 and kw["capture_cap"] == 500
    assert kw["no_match_fresh_days"] == 21
    assert isinstance(kw["active_set"], set)
    assert kw["discovered"] is not None and kw["source_state"] is not None
    # Same follow_redirects posture as the manual CLI — the a16z page redirects.
    assert kw["client"].follow_redirects is True
    assert result["tier"] == "discovery"


@pytest.mark.asyncio
async def test_discovery_tier_vc_inert_without_firms(tmp_path, monkeypatch):
    _vc_handler_env(tmp_path, monkeypatch,
                    _VC_CFG_YAML.replace('  vc_firms: ["a16z", "sequoia"]\n', ""))
    from src.handler import _run

    vc = AsyncMock()
    with patch("src.handler.run_discovery", new=AsyncMock()), \
         patch("src.handler.recover_suppressed", new=AsyncMock()), \
         patch("src.handler.run_vc_discovery", new=vc):
        await _run(tier="discovery")
    vc.assert_not_awaited()


@pytest.mark.asyncio
async def test_vc_discovery_failure_never_skips_recovery(tmp_path, monkeypatch):
    _vc_handler_env(tmp_path, monkeypatch, _VC_CFG_YAML)
    from src.handler import _run

    recover = AsyncMock()
    with patch("src.handler.run_discovery", new=AsyncMock()), \
         patch("src.handler.recover_suppressed", new=recover), \
         patch("src.handler.run_vc_discovery",
               new=AsyncMock(side_effect=RuntimeError("portfolio down"))):
        await _run(tier="discovery")
    recover.assert_awaited_once()


# ---- _discovery_seeds (EU seed opt-in) ----
def test_discovery_seeds_default_is_us_only():
    from src.fingerprint import DEFAULT_SEEDS, load_seeds
    from src.handler import _discovery_seeds
    assert _discovery_seeds(False) == load_seeds(DEFAULT_SEEDS)


def test_discovery_seeds_eu_flag_appends_eu_file():
    from src.fingerprint import DEFAULT_SEEDS, EU_SEEDS, load_seeds
    from src.handler import _discovery_seeds
    seeds = _discovery_seeds(True)
    assert seeds == load_seeds(DEFAULT_SEEDS) + load_seeds(EU_SEEDS)


@pytest.mark.asyncio
async def test_run_skips_when_not_set_up(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("JOB_AGG_SQLITE_PATH", str(tmp_path / "never-configured.db"))
    from src.handler import _run
    with caplog.at_level("INFO", logger="src.handler"):
        result = await _run(tier="ats")
    assert result == {"tier": "ats", "skipped": True, "reason": "not_configured"}
    assert any(r.message == "awaiting_setup" for r in caplog.records)


@pytest.mark.asyncio
async def test_run_uses_an_injected_service():
    from src.handler import _run
    # The per-test DB is seeded by _env; the injected service is not set up.
    result = await _run(tier="ats", service=make_service())
    assert result == {"tier": "ats", "skipped": True, "reason": "not_configured"}


def test_build_sinks_omits_unset_urls():
    from src.config import AppConfig, Secrets
    from src.handler import _build_sinks
    from src.notify.discord import DiscordSink
    from src.notify.ntfy import NtfySink
    assert _build_sinks(AppConfig()) == []
    only_discord = _build_sinks(AppConfig(secrets=Secrets(discord_webhook_url="https://discord.test/x")))
    assert [type(s) for s in only_discord] == [DiscordSink]
    both = _build_sinks(AppConfig(secrets=Secrets(
        ntfy_topic_url="https://ntfy.test/x", discord_webhook_url="https://discord.test/x",
    )))
    assert [type(s) for s in both] == [NtfySink, DiscordSink]


@pytest.mark.asyncio
async def test_run_digest_skips_without_a_discord_url(monkeypatch):
    monkeypatch.delenv("JOB_AGG_DISCORD_WEBHOOK_URL", raising=False)
    seed_settings({"gap_analysis": {"enabled": True}})
    from src.handler import _run
    assert await _run(tier="digest") == {"tier": "digest", "skipped": True}


@pytest.mark.asyncio
async def test_run_builds_llm_features_from_documents(tmp_path, monkeypatch):
    from src.handler import _run
    from src.orchestrator import RunResult
    _sqlite_env(monkeypatch, tmp_path)
    seed_settings(_minimal_cfg_yaml(), documents={"profile": "# my profile", "resume_text": "# my resume"})
    got: dict = {}

    def fake_scorer(cfg, profile_text):
        got["profile"] = profile_text

    def fake_analyzer(cfg, resume_text):
        got["resume"] = resume_text

    async def fake_run_once(**kwargs):
        return RunResult(duration_ms=1)

    monkeypatch.setattr("src.handler._build_relevance_scorer", fake_scorer)
    monkeypatch.setattr("src.handler._build_gap_analyzer", fake_analyzer)
    monkeypatch.setattr("src.handler.run_once", fake_run_once)
    await _run(tier="ats")
    assert got == {"profile": "# my profile", "resume": "# my resume"}


@pytest.mark.asyncio
async def test_run_alerts_config_fallback_when_settings_are_degraded(tmp_path, monkeypatch):
    from src.handler import _run
    from src.orchestrator import RunResult
    from src.stores import build_stores
    _sqlite_env(monkeypatch, tmp_path)
    monkeypatch.setenv("JOB_AGG_OPS_NTFY_TOPIC_URL", "https://ntfy.test/ops")
    bad = build_stores().settings.insert_settings(
        doc={"schedules": {"ats_minutes": 0}}, source="ui", note=None, schema_version=1,
    )
    sent: list = []

    async def fake_send(client, alert, **kwargs):
        sent.append(alert)
        return True

    async def fake_run_once(**kwargs):
        return RunResult(duration_ms=1)

    monkeypatch.setattr("src.handler.send_ops_alert", fake_send)
    monkeypatch.setattr("src.handler.run_once", fake_run_once)
    await _run(tier="ats")
    fallback = [a for a in sent if a.condition == "config_fallback"]
    assert len(fallback) == 1
    assert str(bad) in fallback[0].body


def test_ollama_builders_use_the_configured_host(monkeypatch):
    captured = {}

    class FakeClient:
        def __init__(self, host=None, headers=None):
            captured["host"] = host

    monkeypatch.setattr("ollama.AsyncClient", FakeClient)
    from src import handler
    cfg = _minimal_app_config(relevance_kwargs={
        "enabled": True, "provider": "ollama", "ollama_host": "http://gpu-box:11434",
    })
    assert handler._build_relevance_scorer(cfg, "# profile") is not None
    assert captured["host"] == "http://gpu-box:11434"
