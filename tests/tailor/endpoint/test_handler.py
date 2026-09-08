import json

import src.tailor.endpoint.handler as h
from src.tailor.endpoint.auth import sign_token

SECRET = "s"


def _event(job_id, token, run=False, regen=False):
    qs = {"job_id": job_id, "t": token}
    if run:
        qs["run"] = "1"
    if regen:
        qs["regen"] = "1"
    return {"queryStringParameters": qs}


def _tok(job_id):
    return sign_token(job_id, exp=2_000_000_000, secret=SECRET)


def test_invalid_token_returns_error_page(monkeypatch):
    monkeypatch.setenv("JOB_AGG_TAILOR_SIGNING_SECRET", SECRET)
    resp = h.handler(_event("j", "bad.token"), None)
    assert resp["statusCode"] == 200
    assert "text/html" in resp["headers"]["Content-Type"]
    assert "expired" in resp["body"].lower() or "invalid" in resp["body"].lower()


def test_valid_token_no_run_returns_loading_page(monkeypatch):
    monkeypatch.setenv("JOB_AGG_TAILOR_SIGNING_SECRET", SECRET)
    monkeypatch.setattr(h, "read_jd", lambda table, job_id: None)  # avoid AWS
    resp = h.handler(_event("j", _tok("j")), None)
    assert resp["statusCode"] == 200 and "spinner" in resp["body"].lower()


def test_run_route_dispatches_to_run_tailor(monkeypatch):
    monkeypatch.setenv("JOB_AGG_TAILOR_SIGNING_SECRET", SECRET)
    monkeypatch.setenv("JOB_AGG_TAILOR_BUCKET", "b")
    monkeypatch.setattr(h, "_bootstrap", lambda: (object(), object(), object()))
    monkeypatch.setattr(h, "read_jd", lambda table, job_id: None)
    captured = {}
    def fake_run(**kw):
        captured.update(kw); return {"pdf_url": "u", "cached": False}
    monkeypatch.setattr(h, "run_tailor", fake_run)
    resp = h.handler(_event("j", _tok("j"), run=True, regen=True), None)
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["pdf_url"] == "u"
    assert captured["job_id"] == "j" and captured["regen"] is True


def test_handler_run_uses_s3storage(monkeypatch):
    from src.tailor.endpoint.storage import S3Storage
    captured = {}

    def fake_run_tailor(**kwargs):
        captured.update(kwargs)
        return {"pdf_url": "https://signed", "cached": True}

    monkeypatch.setattr(h, "run_tailor", fake_run_tailor)
    monkeypatch.setattr(h, "_bootstrap", lambda: (object(), object(), object()))
    monkeypatch.setenv("JOB_AGG_TAILOR_SIGNING_SECRET", "s3cr3t")
    monkeypatch.setenv("JOB_AGG_TAILOR_BUCKET", "bkt")
    monkeypatch.setattr(h, "read_jd", lambda *a, **k: None)
    monkeypatch.setattr(h, "verify_token", lambda *a, **k: True)

    event = {"queryStringParameters": {"job_id": "j1", "t": "tok", "run": "1"}}
    resp = h.handler(event, None)
    assert resp["statusCode"] == 200
    assert isinstance(captured["storage"], S3Storage)
