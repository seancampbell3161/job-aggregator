# tests/test_tune_thresholds_cli.py
import sys

import pytest

from src.sqlite_db import connect
from src.state_sqlite import SqliteRejectedPostingsStore, SqliteSeenJobsStore
from tests.settings_helpers import seed_settings


def _seed(db_path):
    conn = connect(str(db_path))
    seen = SqliteSeenJobsStore(conn)
    rejected = SqliteRejectedPostingsStore(conn)
    from src.models import NormalizedPosting
    from datetime import datetime, timezone

    def p(job_id, title, score):
        return NormalizedPosting(
            job_id=job_id, title=title, company="Acme", location_text="Remote (US)",
            location_tags=frozenset(), seniority="mid", stack=frozenset(),
            comp_min=None, comp_max=None, apply_url=f"https://a/{job_id}",
            description="d", posted_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
            source="greenhouse:acme",
        )
    # 12 suppressed rows: rescued at 4, confirmed at 3 → unique perfect split
    # at t=3 (surface the 4s, keep suppressing <=3) → suggested score_low: 3.
    for i in range(6):
        seen.mark_suppressed(f"s:r{i}", score=4, rationale="x", posting=p(f"s:r{i}", "Eng", 4))
        seen.set_audit_verdict(f"s:r{i}", "rescued")
        seen.mark_suppressed(f"s:c{i}", score=3, rationale="x", posting=p(f"s:c{i}", "Eng", 3))
        seen.set_audit_verdict(f"s:c{i}", "confirmed_rejected")
    return db_path


def test_cli_reports_score_low_suggestion(tmp_path, monkeypatch, capsys):
    db = _seed(tmp_path / "t.db")
    monkeypatch.setenv("JOB_AGG_SQLITE_PATH", str(db))
    seed_settings({"relevance": {"score_low": 4}, "filters": {"titles": ["software engineer"]}})
    from scripts.tune_thresholds import run
    rc = run(["--min-verdicts", "10"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "score_low: 3" in out  # the suggested lowered threshold


def test_cli_reads_rescued_via_transition_rows(tmp_path, monkeypatch, capsys):
    """Regression: the /audit rescue route for a score_low-origin job used to
    delete the suppressed row and re-notify WITHOUT recording the rescue
    verdict or the original suppressed score, so analyze_score_low could never
    see a rescued sample. Seed one confirmed-suppressed row (score 3) AND one
    rescued-via-transition row (original score 4, released + re-claimed, then
    stamped via mark_rescued_from_suppression) and confirm the CLI reads both
    and is confident (both classes present)."""
    db = tmp_path / "t.db"
    conn = connect(str(db))
    seen = SqliteSeenJobsStore(conn)
    from datetime import datetime, timezone

    from src.models import NormalizedPosting

    def p(job_id, title):
        return NormalizedPosting(
            job_id=job_id, title=title, company="Acme", location_text="Remote (US)",
            location_tags=frozenset(), seniority="mid", stack=frozenset(),
            comp_min=None, comp_max=None, apply_url=f"https://a/{job_id}",
            description="d", posted_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
            source="greenhouse:acme",
        )

    # confirmed suppressed row, never rescued: score 3
    seen.mark_suppressed("c:1", score=3, rationale="x", posting=p("c:1", "Eng"))
    seen.set_audit_verdict("c:1", "confirmed_rejected")

    # rescued-via-transition row: originally suppressed at score 4
    seen.mark_suppressed("r:1", score=4, rationale="x", posting=p("r:1", "Eng"))
    seen.release_claim("r:1")
    seen.claim_for_notify("r:1", score=8, posting=p("r:1", "Eng"))
    seen.mark_rescued_from_suppression("r:1", suppressed_score=4)

    monkeypatch.setenv("JOB_AGG_SQLITE_PATH", str(db))
    seed_settings({"relevance": {"score_low": 4}, "filters": {"titles": ["software engineer"]}})
    from scripts.tune_thresholds import run
    rc = run(["--min-verdicts", "2"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "samples: 2 (rescued [4], confirmed [3])" in out
    assert "score_low: 3" in out  # the suggested lowered threshold


def test_cli_insufficient_data_exits_zero(tmp_path, monkeypatch, capsys):
    db = connect(str(tmp_path / "empty.db"))  # schema, no rows
    monkeypatch.setenv("JOB_AGG_SQLITE_PATH", str(tmp_path / "empty.db"))
    seed_settings({"relevance": {"score_low": 4}, "filters": {"titles": ["software engineer"]}})
    from scripts.tune_thresholds import run
    rc = run([])
    assert rc == 0
    assert "insufficient" in capsys.readouterr().out.lower()


def test_cli_since_rejects_unparseable_date(tmp_path, monkeypatch, capsys):
    from scripts.tune_thresholds import run
    with pytest.raises(SystemExit):
        run(["--since", "6/1/2026"])


def test_cli_uses_defaults_when_not_set_up(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("JOB_AGG_SQLITE_PATH", str(tmp_path / "never-configured.db"))
    from scripts.tune_thresholds import run
    assert run([]) == 0
    assert "current: 3" in capsys.readouterr().out  # RelevanceConfig.score_low default


def test_cli_reports_source_stats_and_snippet_pairs(tmp_path, monkeypatch, capsys):
    from datetime import datetime, timezone
    from src.models import NormalizedPosting
    from src.state_sqlite import SqliteSeenJobsStore

    conn = connect(str(tmp_path / "t.db"))
    seen = SqliteSeenJobsStore(conn)

    def posting(job_id, source):
        return NormalizedPosting(
            job_id=job_id, title="Staff Software Engineer", company="Acme Robotics",
            location_text="Austin, TX", location_tags=frozenset(), seniority="staff",
            stack=frozenset(), comp_min=None, comp_max=None,
            apply_url=f"https://a/{job_id}", description="d",
            posted_at=datetime(2026, 7, 17, tzinfo=timezone.utc), source=source,
        )

    seen.claim_for_notify("adzuna:5001", score=5, posting=posting("adzuna:5001", "adzuna"))
    seen.claim_for_notify("greenhouse:acme:9", score=7,
                          posting=posting("greenhouse:acme:9", "greenhouse:acme"))

    monkeypatch.setenv("JOB_AGG_SQLITE_PATH", str(tmp_path / "t.db"))
    seed_settings({"relevance": {"score_low": 4}, "filters": {"titles": ["software engineer"]}})
    from scripts.tune_thresholds import run
    rc = run([])
    assert rc == 0
    out = capsys.readouterr().out
    assert "=== score by source ===" in out
    assert "adzuna" in out and "greenhouse" in out
    assert "=== adzuna snippet pairs ===" in out
    assert "delta +2" in out
