from __future__ import annotations

import os
import sys
from pathlib import Path

import uvicorn
import yaml

from src.config import BoardConfig, KitConfig, RelevanceConfig
from src.web.app import create_app


def _score_thresholds(config_path: str) -> tuple[int, int]:
    """Read the relevance score thresholds (used only for badge colors) from
    config.yaml WITHOUT pulling in notification secrets.

    The triage UI never notifies, so it must not require the ntfy/Discord env
    vars that the full ``load_config()`` hard-requires — a triage-only user has
    no reason to set them. Falls back to RelevanceConfig defaults if the file or
    its ``relevance`` block is missing or unreadable."""
    try:
        raw = yaml.safe_load(Path(config_path).read_text()) or {}
        rel = RelevanceConfig(**(raw.get("relevance") or {}))
    except (OSError, yaml.YAMLError, ValueError, TypeError):
        rel = RelevanceConfig()
    return rel.score_high, rel.score_low


def _stale_after_days(config_path: str) -> int:
    """Read board.stale_after_days from config.yaml without requiring secrets."""
    try:
        raw = yaml.safe_load(Path(config_path).read_text()) or {}
        return BoardConfig(**(raw.get("board") or {})).stale_after_days
    except (OSError, yaml.YAMLError, ValueError, TypeError):
        return BoardConfig().stale_after_days


def _kit_facts_path(config_path: str) -> str:
    """Read kit.facts_path from config.yaml without requiring secrets."""
    try:
        raw = yaml.safe_load(Path(config_path).read_text()) or {}
        return KitConfig(**(raw.get("kit") or {})).facts_path
    except (OSError, yaml.YAMLError, ValueError, TypeError):
        return KitConfig().facts_path


def _startup_repo_ok(app) -> bool:
    """Verify the repo (DynamoDB or SQLite) is reachable before binding the port,
    so a creds/path problem prints a friendly one-liner instead of a stack trace
    on first click."""
    try:
        app.state.repo.list()
        return True
    except Exception as exc:  # noqa: BLE001 — friendly startup diagnostic
        backend = os.environ.get("JOB_AGG_BACKEND", "sqlite")
        if backend == "dynamodb":
            hint = ("cannot reach the seen_jobs DynamoDB table.\n"
                    "  Check your AWS credentials and JOB_AGG_SEEN_JOBS_TABLE.\n")
        else:
            path = os.environ.get("JOB_AGG_SQLITE_PATH", "data/job_aggregator.db")
            hint = (f"cannot open the local SQLite DB at {path}.\n"
                    "  Check JOB_AGG_SQLITE_PATH and the ./data volume mount.\n")
        sys.stderr.write(f"job-aggregator web: {hint}  ({type(exc).__name__}: {exc})\n")
        return False


def main() -> int:
    cfg_path = os.environ.get("JOB_AGG_CONFIG_PATH", "config.yaml")
    score_high, score_low = _score_thresholds(cfg_path)
    stale_after_days = _stale_after_days(cfg_path)
    app = create_app(score_high=score_high, score_low=score_low,
                     stale_after_days=stale_after_days,
                     kit_facts_path=_kit_facts_path(cfg_path))
    if not _startup_repo_ok(app):
        return 1
    host = os.environ.get("JOB_AGG_WEB_HOST", "127.0.0.1")
    port = int(os.environ.get("JOB_AGG_WEB_PORT", "8000"))
    uvicorn.run(app, host=host, port=port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
