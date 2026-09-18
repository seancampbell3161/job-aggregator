"""Test buttons.

Each probe drives the same class production drives — NtfySink, DiscordSink, the
scorer the orchestrator builds — so a green result means the real path works.
Probes never persist anything: they are handed the values in the form."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx

from src.config import AppConfig
# The orchestrator's private scorer factory (see src/handler.py) — imported
# directly rather than duplicated, so the probe and the poller can never score
# a posting differently. Kept under this module's own private name so the
# rest of this file (and its tests) don't need to know handler.py's name for it.
from src.handler import _build_relevance_scorer as _build_scorer
from src.models import NormalizedPosting
from src.notify.base import NotificationPayload
from src.notify.discord import DiscordSink
from src.notify.ntfy import NtfySink

_TIMEOUT = 10.0


@dataclass(frozen=True)
class ProbeResult:
    ok: bool
    detail: str


def _transport() -> httpx.BaseTransport | None:
    """Seam for tests; production uses httpx's default transport."""
    return None


SAMPLE_PAYLOAD = NotificationPayload(
    title="Test from job-aggregator",
    company="job-aggregator",
    role="Settings test",
    location="Remote",
    comp=None,
    stack_matched=[],
    posted="just now",
    apply_url="https://github.com/seancampbell3161/job-aggregator",
    source="settings-test",
    seniority=None,
    relevance_rationale="This is a test notification you triggered from Settings.",
)

SAMPLE_POSTING = NormalizedPosting(
    job_id="settings-test:1",
    title="Senior Platform Engineer",
    company="Example Corp",
    location_text="Remote (US)",
    location_tags=frozenset({"remote"}),
    seniority="senior",
    stack=frozenset({"python", "kubernetes"}),
    comp_min=180000,
    comp_max=220000,
    apply_url="https://example.com/jobs/1",
    description=(
        "We are hiring a senior platform engineer to own our Kubernetes "
        "estate, developer tooling, and CI/CD. Python and Go."
    ),
    posted_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    source="settings-test",
)


def _check_url(url: str) -> ProbeResult | None:
    stripped = url.strip()
    if not stripped:
        return ProbeResult(False, "No URL to test — fill the field first.")
    scheme = urlparse(stripped).scheme.lower()
    if scheme not in ("http", "https"):
        return ProbeResult(False, f"Only http and https URLs can be tested (got {scheme or 'none'}).")
    return None


async def _send(sink, url: str, success: str) -> ProbeResult:
    bad = _check_url(url)
    if bad is not None:
        return bad
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, transport=_transport()) as client:
            await sink.send(client, SAMPLE_PAYLOAD)
    except httpx.HTTPStatusError as exc:
        return ProbeResult(False, f"Rejected with HTTP {exc.response.status_code}.")
    except httpx.HTTPError as exc:
        return ProbeResult(False, f"Could not reach it: {exc}.")
    except Exception as exc:  # noqa: BLE001 — a probe reports, it never 500s the page
        return ProbeResult(False, f"{type(exc).__name__}: {exc}")
    return ProbeResult(True, success)


async def probe_ntfy(url: str) -> ProbeResult:
    return await _send(
        NtfySink(topic_url=url, quiet_hours=None), url,
        "Sent — check your phone. (Quiet hours are ignored for tests.)",
    )


async def probe_discord(url: str) -> ProbeResult:
    return await _send(
        DiscordSink(webhook_url=url), url, "Sent — check the Discord channel.",
    )


async def probe_llm(cfg: AppConfig, profile: str | None) -> ProbeResult:
    """Score one synthetic posting with the configured provider."""
    try:
        scorer = _build_scorer(cfg, profile)
    except Exception as exc:  # noqa: BLE001 — a probe reports, it never 500s the page.
        # _build_scorer eagerly constructs a provider client (e.g. the Ollama SDK
        # parses ollama_host into an httpx.Client at __init__), and those fields
        # are free-form user-entered config with no format validation upstream —
        # a malformed host/URL raises here, before any scoring is attempted.
        return ProbeResult(False, f"{type(exc).__name__}: {exc}")
    if scorer is None:
        return ProbeResult(False, (
            "Scoring is not configured — it needs to be enabled, with an API "
            "key for the selected provider and a saved profile document."
        ))
    try:
        score = await scorer.score(SAMPLE_POSTING)
    except Exception as exc:  # noqa: BLE001 — surface the provider's message verbatim.
        # All three real scorer implementations fail open and never raise out of
        # score() (see src/relevance.py's module docstring) — this branch is
        # defence-in-depth for a test double or a future implementation that
        # violates that contract, not something the shipped scorers exercise.
        return ProbeResult(False, f"{type(exc).__name__}: {exc}")
    # The real scorer implementations fail open (see src/relevance.py): a
    # provider error never raises out of score(), it comes back as a sentinel
    # Score with is_fallback=True. A probe that only watched for a raised
    # exception would report that as a green result.
    if score.is_fallback:
        return ProbeResult(False, f"Provider returned no score ({score.error_type}): {score.rationale}")
    return ProbeResult(True, (
        f"Scored the sample posting {score.value}/10 — {score.rationale}"
    ))
