from __future__ import annotations

import os
import sys

import uvicorn

from src.web.app import create_app


def _startup_repo_ok(app) -> bool:
    """Verify the SQLite DB — jobs and settings — is readable before binding
    the port, so a path/permission problem prints a friendly one-liner
    instead of a stack trace on first click."""
    try:
        app.state.repo.list()
        app.state.service.snapshot()
        return True
    except Exception as exc:  # noqa: BLE001 — friendly startup diagnostic
        path = os.environ.get("JOB_AGG_SQLITE_PATH", "data/job_aggregator.db")
        sys.stderr.write(
            f"job-aggregator web: cannot open the local SQLite DB at {path}.\n"
            "  Check JOB_AGG_SQLITE_PATH and the ./data volume mount.\n"
            f"  ({type(exc).__name__}: {exc})\n"
        )
        return False


def main() -> int:
    app = create_app()
    if not _startup_repo_ok(app):
        return 1
    try:
        app.state.service.ensure_signing_secret()
    except Exception as exc:  # noqa: BLE001 — deep links degrade; the UI must still start
        sys.stderr.write(
            f"job-aggregator web: could not bootstrap the tailor signing secret "
            f"({type(exc).__name__}: {exc})\n"
        )
    host = os.environ.get("JOB_AGG_WEB_HOST", "127.0.0.1")
    port = int(os.environ.get("JOB_AGG_WEB_PORT", "8000"))
    uvicorn.run(app, host=host, port=port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
