"""One place to decide how this project identifies itself over HTTP.

The default is honest: a product token plus a contact URL, which is what a
well-behaved automated client is expected to send. Every outbound request in
the project routes through here so that identity is a single, auditable
decision rather than eleven copy-pasted string literals.

``http.user_agent`` in config.yaml lets an operator identify their own
deployment differently (for example, with their own contact URL). Whoever sets
it owns that choice, including whether the value is consistent with the target
site's terms of use. See docs/CONFIG.md.
"""
from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

PROJECT_URL = "https://github.com/seancampbell3161/job-aggregator"

try:
    _VERSION = version("job-aggregator")
except PackageNotFoundError:  # source checkout that was never pip-installed
    _VERSION = "dev"

DEFAULT_USER_AGENT = f"job-aggregator/{_VERSION} (+{PROJECT_URL})"

_current = DEFAULT_USER_AGENT


def set_user_agent(ua: str | None) -> None:
    """Install the operator's override; ``None``/blank restores the default.

    Called once from ``load_config`` so every module picks it up without
    threading config through call sites that never otherwise need it."""
    global _current
    _current = ua.strip() if ua and ua.strip() else DEFAULT_USER_AGENT


def user_agent() -> str:
    """The User-Agent every outbound request should carry."""
    return _current


def headers(**extra: str) -> dict[str, str]:
    """Request headers carrying the current UA, plus any per-call extras.

    Built per call, not at import time, so a config override applied after
    module import is still honoured."""
    return {"User-Agent": _current, **extra}
