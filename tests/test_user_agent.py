"""The project's outbound identity is one decision, made in one place."""
import pytest

from src.user_agent import (
    DEFAULT_USER_AGENT,
    headers,
    set_user_agent,
    user_agent,
)


@pytest.fixture(autouse=True)
def _restore_default():
    yield
    set_user_agent(None)


def test_default_is_honest_and_carries_a_contact_url():
    assert DEFAULT_USER_AGENT.startswith("job-aggregator/")
    assert "github.com/seancampbell3161/job-aggregator" in DEFAULT_USER_AGENT
    assert "Mozilla" not in DEFAULT_USER_AGENT


def test_override_is_applied_and_blank_restores_the_default():
    set_user_agent("custom-agent/2.0")
    assert user_agent() == "custom-agent/2.0"
    set_user_agent("   ")
    assert user_agent() == DEFAULT_USER_AGENT
    set_user_agent("  padded/1.0  ")
    assert user_agent() == "padded/1.0"
    set_user_agent(None)
    assert user_agent() == DEFAULT_USER_AGENT


def test_headers_are_built_per_call_so_a_later_override_is_honoured():
    before = headers(Accept="application/json")
    set_user_agent("custom-agent/2.0")
    after = headers(Accept="application/json")
    assert before["User-Agent"] == DEFAULT_USER_AGENT
    assert after["User-Agent"] == "custom-agent/2.0"
    assert after["Accept"] == "application/json"


def test_no_module_ships_a_hardcoded_browser_user_agent():
    """Regression guard for the spoofed-UA default: src/ must not reintroduce a
    browser UA string outside the headless browser tier, which drives a real
    Chromium and is governed separately."""
    from pathlib import Path

    src = Path(__file__).resolve().parent.parent / "src"
    offenders = [
        f.relative_to(src.parent).as_posix()
        for f in src.rglob("*.py")
        if f.name != "headless.py" and "Mozilla/5.0" in f.read_text()
    ]
    assert not offenders, f"hardcoded browser User-Agent reintroduced in: {offenders}"
