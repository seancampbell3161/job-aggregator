"""/healthz: 200 in every setup state, past every gate, without touching settings."""
from src.sqlite_db import connect
from src.web.app import create_app
from tests.auth_helpers import sign_in, signed_in_client
from tests.settings_helpers import configured_stores, make_service
from tests.sqlite_helpers import sqlite_stores

import pytest
from fastapi.testclient import TestClient


def _client(tmp_path, monkeypatch, service, stores=None):
    # Deliberately NOT signed in: a healthcheck runs without credentials.
    monkeypatch.setenv("JOB_AGG_SQLITE_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("JOB_AGG_TAILORED_DIR", str(tmp_path / "tailored"))
    stores = stores if stores is not None else sqlite_stores(connect(":memory:"))
    return TestClient(create_app(stores=stores, service=service), follow_redirects=False)


def test_healthz_is_ok_before_setup(tmp_path, monkeypatch):
    r = _client(tmp_path, monkeypatch, make_service(), sqlite_stores(connect(":memory:"))).get("/healthz")
    assert r.status_code == 200
    assert r.text == "ok"


def test_healthz_is_ok_after_setup(tmp_path, monkeypatch):
    conn = connect(":memory:")
    stores = configured_stores(conn)
    r = _client(tmp_path, monkeypatch, make_service(conn=conn, doc={"relevance": {"score_low": 4}}), stores).get("/healthz")
    assert r.status_code == 200
    assert r.text == "ok"


def test_healthz_does_not_read_settings(tmp_path, monkeypatch):
    """It is a liveness probe: it must answer even when the settings store is
    unreadable, which is exactly when a readiness probe would be wrong."""
    service = make_service()
    client = _client(tmp_path, monkeypatch, service, sqlite_stores(connect(":memory:")))

    def boom():
        raise RuntimeError("settings store is unreadable")

    # Patched AFTER create_app, so a failure is unambiguously the request path
    # rather than app construction.
    monkeypatch.setattr(service, "snapshot", boom)

    assert client.get("/healthz").status_code == 200
    # Control: the same broken service must break an ordinary page, or this
    # test proves nothing. TestClient re-raises server exceptions rather than
    # returning 500, so assert the exception, not a status code.
    # Must be signed in to reach the snapshot middleware (otherwise login gate catches it).
    control_client = signed_in_client(client.app, follow_redirects=False)
    with pytest.raises(RuntimeError):
        control_client.get("/jobs")


def test_healthz_matches_whole_path_only(tmp_path, monkeypatch):
    client = sign_in(_client(tmp_path, monkeypatch, make_service()))
    r = client.get("/healthzz")
    assert r.status_code == 303
    assert r.headers["location"] == "/setup"
