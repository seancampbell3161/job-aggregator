import os

import pytest


def _weasyprint_unavailable() -> str | None:
    """Why WeasyPrint can't be used here, or None if it can.

    NOT ``pytest.importorskip``: WeasyPrint imports fine as a Python package
    but raises **OSError** from cffi when its native libraries (pango, cairo,
    gobject) are absent — the usual case on a fresh macOS checkout. OSError is
    not an ImportError, so importorskip lets it through and the whole module
    errors during collection, which aborts the entire test session. Catching
    Exception here is deliberate."""
    try:
        import weasyprint  # noqa: F401
    except Exception as exc:  # noqa: BLE001 — any failure means "unusable"
        first = (str(exc) or type(exc).__name__).strip().splitlines()[0]
        return first[:120]
    return None


WEASYPRINT_UNAVAILABLE = _weasyprint_unavailable()

requires_weasyprint = pytest.mark.skipif(
    WEASYPRINT_UNAVAILABLE is not None,
    reason=f"needs WeasyPrint + native libs ({WEASYPRINT_UNAVAILABLE})",
)


@pytest.fixture(autouse=True)
def _sqlite_path_isolated(monkeypatch, tmp_path):
    """Never let a test open ./data/job_aggregator.db in the checkout.

    ``src.sqlite_db`` defaults to that path when ``JOB_AGG_SQLITE_PATH`` is
    unset, and docker-compose bind-mounts ./data — so an unguarded test would
    open (and migrate) a live database. Point it at a per-test temp file
    instead. Tests that set the variable themselves still win: their
    ``monkeypatch.setenv`` runs after this autouse fixture.
    """
    monkeypatch.setenv("JOB_AGG_SQLITE_PATH", str(tmp_path / "test.db"))
