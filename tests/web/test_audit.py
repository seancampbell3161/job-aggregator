# tests/web/test_audit.py
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from src.models import NormalizedPosting
from src.sqlite_db import connect
from src.web.app import create_app
from src.web.repo import TriageRepo
from tests.sqlite_helpers import sqlite_stores


def _posting(job_id, title, company="Acme"):
    return NormalizedPosting(
        job_id=job_id, title=title, company=company, location_text="Remote (US)",
        location_tags=frozenset({"remote", "us"}), seniority="mid",
        stack=frozenset({"python"}), comp_min=None, comp_max=None,
        apply_url=f"https://apply/{job_id}", description="Ship Python services.",
        posted_at=datetime(2026, 7, 1, tzinfo=timezone.utc), source="greenhouse:acme",
    )


@pytest.fixture
def audit_client(tmp_path, monkeypatch):
    monkeypatch.setenv("JOB_AGG_TAILORED_DIR", str(tmp_path / "tailored"))
    monkeypatch.delenv("JOB_AGG_OPS_NTFY_TOPIC_URL", raising=False)
    monkeypatch.delenv("JOB_AGG_OPS_DISCORD_WEBHOOK_URL", raising=False)
    conn = connect(":memory:")
    stores = sqlite_stores(conn)
    seen = stores.seen
    rejected = stores.rejected
    rejected.record(_posting("greenhouse:acme:1", "Office Manager"), rejected_by="role")
    seen.mark_suppressed(
        "greenhouse:acme:2", score=3, rationale="Weak fit",
        posting=_posting("greenhouse:acme:2", "Data Engineer"),
    )
    app = create_app(repo=TriageRepo(seen), stores=stores)
    return TestClient(app), rejected, seen


def test_audit_lists_both_origins_with_reasons(audit_client):
    client, _, _ = audit_client
    r = client.get("/audit")
    assert r.status_code == 200
    assert "Office Manager" in r.text
    assert "filter: role" in r.text
    assert "Data Engineer" in r.text
    assert "score 3/10" in r.text and "Weak fit" in r.text


def test_audit_tally_counts_by_gate(audit_client):
    client, _, _ = audit_client
    r = client.get("/audit")
    assert ">role<" in r.text or "role</a>" in r.text  # gate chip present
    assert "score_low" in r.text


def test_audit_gate_filter(audit_client):
    client, _, _ = audit_client
    r = client.get("/audit", params={"gate": "role"})
    assert "Office Manager" in r.text
    assert "Data Engineer" not in r.text
    r = client.get("/audit", params={"gate": "score_low"})
    assert "Data Engineer" in r.text
    assert "Office Manager" not in r.text


def test_audit_text_search(audit_client):
    client, _, _ = audit_client
    r = client.get("/audit", params={"q": "office"})
    assert "Office Manager" in r.text
    assert "Data Engineer" not in r.text


def test_pipeline_shows_rejected_stat_linking_audit(audit_client):
    client, _, _ = audit_client
    r = client.get("/pipeline")
    assert r.status_code == 200
    assert 'href="/audit"' in r.text
    assert "rejected /7d" in r.text


def test_rescue_filter_reject_promotes_to_triage(audit_client):
    client, rejected, seen = audit_client
    r = client.post("/audit/rescue", params={"id": "greenhouse:acme:1"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/audit"
    m = seen.get_match("greenhouse:acme:1")
    assert m is not None and m["title"] == "Office Manager"
    assert m["status"] == "new"
    assert rejected.get("greenhouse:acme:1")["verdict"] == "rescued"
    # default audit view no longer shows the judged row
    assert "Office Manager" not in client.get("/audit").text


def test_rescue_suppressed_row_promotes_and_unsuppresses(audit_client):
    client, _, seen = audit_client
    r = client.post("/audit/rescue", params={"id": "greenhouse:acme:2"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert seen.get_match("greenhouse:acme:2") is not None
    assert seen.get_suppressed("greenhouse:acme:2") is None


def test_rescue_score_low_origin_records_verdict_and_original_score(audit_client):
    """Regression: the score_low rescue path used to delete the suppressed row
    and re-notify WITHOUT recording a rescue verdict or the original suppressed
    score, so the tuning CLI could never see a rescued sample."""
    client, _, seen = audit_client
    r = client.post("/audit/rescue", params={"id": "greenhouse:acme:2"},
                    follow_redirects=False)
    assert r.status_code == 303
    rescued = seen.list_rescued_suppressions(since_iso="")
    assert len(rescued) == 1
    assert rescued[0]["score"] == 3  # the ORIGINAL suppressed score
    assert rescued[0]["audit_verdict"] == "rescued"


def test_rescue_dual_origin_promotes_to_triage(audit_client):
    # Real workflow: posting filter-rejected, then re-fetched and passed a
    # widened filter, then LLM-suppressed -- job_id ends up in BOTH stores.
    client, rejected, seen = audit_client
    job_id = "greenhouse:acme:3"
    rejected.record(_posting(job_id, "Dual Origin"), rejected_by="role")
    seen.mark_suppressed(
        job_id, score=3, rationale="Weak fit",
        posting=_posting(job_id, "Dual Origin"),
    )

    r = client.post("/audit/rescue", params={"id": job_id}, follow_redirects=False)

    assert r.status_code == 303
    assert seen.get_match(job_id) is not None
    assert seen.get_suppressed(job_id) is None
    assert rejected.get(job_id)["verdict"] == "rescued"


def test_dual_origin_row_renders_once_on_audit_page(audit_client):
    client, rejected, seen = audit_client
    job_id = "greenhouse:acme:4"
    rejected.record(_posting(job_id, "Dual Origin Row"), rejected_by="role")
    seen.mark_suppressed(
        job_id, score=3, rationale="Weak fit",
        posting=_posting(job_id, "Dual Origin Row"),
    )

    r = client.get("/audit")

    assert r.text.count("Dual Origin Row") == 1


def test_rescue_unknown_id_404s(audit_client):
    client, _, _ = audit_client
    assert client.post("/audit/rescue", params={"id": "nope:1"},
                       follow_redirects=False).status_code == 404


def test_confirm_marks_filter_reject(audit_client):
    client, rejected, _ = audit_client
    r = client.post("/audit/confirm", params={"id": "greenhouse:acme:1"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert rejected.get("greenhouse:acme:1")["verdict"] == "confirmed_rejected"
    # visible again with the judged toggle
    assert "Office Manager" in client.get("/audit", params={"judged": "true"}).text


def test_confirm_marks_suppressed_row(audit_client):
    client, _, seen = audit_client
    r = client.post("/audit/confirm", params={"id": "greenhouse:acme:2"},
                    follow_redirects=False)
    assert r.status_code == 303
    it = seen.get_suppressed("greenhouse:acme:2")
    assert it["audit_verdict"] == "confirmed_rejected"


def test_confirm_unknown_id_404s(audit_client):
    client, _, _ = audit_client
    assert client.post("/audit/confirm", params={"id": "nope:1"},
                       follow_redirects=False).status_code == 404


def test_confirm_dual_origin_marks_both_stores(audit_client):
    # Real workflow: posting filter-rejected, then re-fetched and passed a
    # widened filter, then LLM-suppressed -- job_id ends up in BOTH stores.
    client, rejected, seen = audit_client
    job_id = "greenhouse:acme:5"
    rejected.record(_posting(job_id, "Dual Origin Confirm"), rejected_by="role")
    seen.mark_suppressed(
        job_id, score=3, rationale="Weak fit",
        posting=_posting(job_id, "Dual Origin Confirm"),
    )

    r = client.post("/audit/confirm", params={"id": job_id}, follow_redirects=False)

    assert r.status_code == 303
    assert rejected.get(job_id)["verdict"] == "confirmed_rejected"
    assert seen.get_suppressed(job_id)["audit_verdict"] == "confirmed_rejected"
    # default audit view no longer shows the judged row...
    assert "Dual Origin Confirm" not in client.get("/audit").text
    # ...but the judged toggle still surfaces it
    assert "Dual Origin Confirm" in client.get("/audit", params={"judged": "true"}).text
