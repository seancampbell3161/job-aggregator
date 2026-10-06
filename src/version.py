"""The running app's version: the one place every module reads it from.

It is the installed package's version (pyproject.toml), so a release bumps
it. A source checkout that was never pip-installed reports "dev"."""
from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    APP_VERSION: str = version("job-aggregator")
except PackageNotFoundError:  # source checkout that was never pip-installed
    APP_VERSION = "dev"
