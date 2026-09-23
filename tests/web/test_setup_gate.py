"""The not-set-up gate, per-request snapshots, and the degraded banner."""
import io
import zipfile
from datetime import datetime, timezone

from src.models import NormalizedPosting
from src.settings.service import ConfigService
from src.settings.store import SqliteSettingsStore
from src.sqlite_db import connect
from src.web.app import create_app
from tests.auth_helpers import signed_in_client
from tests.settings_helpers import configured_stores, make_service
from tests.sqlite_helpers import sqlite_stores


def _client(tmp_path, monkeypatch, service, stores=None):
    monkeypatch.setenv("JOB_AGG_TAILORED_DIR", str(tmp_path / "tailored"))
    stores = stores if stores is not None else sqlite_stores(connect(":memory:"))
    return signed_in_client(create_app(stores=stores, service=service), follow_redirects=False)


def test_pages_redirect_to_setup_until_configured(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, make_service())
    for path in ("/", "/jobs", "/board", "/pipeline", "/audit", "/kit", "/builder"):
        r = client.get(path)
        assert r.status_code == 303, path
        assert r.headers["location"] == "/setup"
    r = client.post("/status", params={"id": "x", "status": "applied"})
    assert r.status_code == 409
    assert "/setup" in r.text


def test_setup_page_shows_the_import_command(tmp_path, monkeypatch):
    r = _client(tmp_path, monkeypatch, make_service()).get("/setup")
    assert r.status_code == 200
    assert "python -m src.settings import" in r.text


def test_exempt_prefixes_are_not_redirected(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, make_service())
    assert client.get("/static/css/tokens.css").status_code == 200
    r = client.get("/tailor", params={"job_id": "j1", "t": "bad"})
    assert r.status_code == 200  # the route's own invalid-link page, not a redirect
    r = client.get("/tailor/pdf", params={"job_id": "j1", "t": "bad"})
    assert r.status_code == 403  # the route's own invalid-link page, not a redirect


def test_exemptions_match_whole_path_segments_only(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, make_service())
    assert client.get("/static/css/tokens.css").status_code == 200
    for path in ("/setupfoo", "/tailor-history", "/statically", "/tailoredx", "/tailored/x.pdf"):
        r = client.get(path)
        assert r.status_code == 303, path
        assert r.headers["location"] == "/setup"


def test_setup_redirects_home_once_configured(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, make_service({}))
    r = client.get("/setup")
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    assert client.get("/").status_code == 200


def test_settings_saved_elsewhere_apply_without_restart(tmp_path, monkeypatch):
    service = make_service()
    client = _client(tmp_path, monkeypatch, service)
    assert client.get("/").status_code == 303
    service.save_settings({}, source="cli")  # e.g. `python -m src.settings import` from another process
    assert client.get("/").status_code == 200


def test_score_thresholds_come_from_the_request_snapshot(tmp_path, monkeypatch):
    conn = connect(":memory:")
    stores = configured_stores(conn, {"relevance": {"score_high": 9}})
    stores.seen.claim_for_notify(
        "greenhouse:acme:1", score=8, rationale="fit",
        posting=NormalizedPosting(
            job_id="greenhouse:acme:1", title="Senior Engineer", company="Acme",
            location_text="Remote (US)", location_tags=frozenset(), seniority="senior",
            stack=frozenset(), comp_min=None, comp_max=None, apply_url="https://a/1",
            description="", posted_at=datetime(2026, 6, 16, tzinfo=timezone.utc),
            source="greenhouse:acme",
        ),
    )
    service = ConfigService(stores.settings, env={})
    client = _client(tmp_path, monkeypatch, service, stores=stores)
    assert 'class="score s-mid"' in client.get("/jobs").text
    service.save_settings({"relevance": {"score_high": 7}}, source="cli")
    assert 'class="score s-high"' in client.get("/jobs").text


def test_degraded_banner_renders(tmp_path, monkeypatch):
    store = SqliteSettingsStore(connect(":memory:"))
    service = ConfigService(store, env={})
    good = service.save_settings({}, source="cli")
    bad = store.insert_settings(doc={"schedules": {"ats_minutes": 0}}, source="ui",
                                note=None, schema_version=1)
    r = _client(tmp_path, monkeypatch, service).get("/")
    assert r.status_code == 200
    assert "config-banner" in r.text
    assert f"Settings version {bad} is invalid" in r.text
    assert f"running on version {good}" in r.text


def test_no_banner_when_healthy(tmp_path, monkeypatch):
    r = _client(tmp_path, monkeypatch, make_service({})).get("/")
    assert "config-banner" not in r.text


def test_setup_offers_a_start_from_defaults_button(tmp_path, monkeypatch):
    r = _client(tmp_path, monkeypatch, make_service()).get("/setup")
    assert r.status_code == 200
    assert 'action="/setup/start"' in r.text
    assert "python -m src.settings import" in r.text  # the CLI path stays


def test_start_from_defaults_creates_a_version_and_opens_settings(tmp_path, monkeypatch):
    service = make_service()
    client = _client(tmp_path, monkeypatch, service)
    r = client.post("/setup/start")
    assert r.status_code == 303
    assert r.headers["location"] == "/settings/filters"
    snap = service.snapshot()
    assert snap is not None
    assert snap.cfg.filters.titles == []


def test_start_from_defaults_is_a_no_op_when_already_set_up(tmp_path, monkeypatch):
    service = make_service({"filters": {"titles": ["staff engineer"]}})
    before = service.current_config()[0]
    r = _client(tmp_path, monkeypatch, service).post("/setup/start")
    assert r.status_code == 303
    assert service.current_config()[0] == before
    assert service.snapshot().cfg.filters.titles == ["staff engineer"]


def test_start_from_defaults_needs_a_session(tmp_path, monkeypatch):
    # /setup/start is exempt from the setup gate but NOT from the login gate,
    # so a password must be claimed (and a session held) before it can write
    # anything. An anonymous POST without a session cookie — regardless of
    # whether a password has been claimed yet — is denied 401 by the login
    # gate itself (Starlette's established convention here: see
    # test_other_methods_get_401 in test_auth_gate.py), before the route ever
    # runs. If this guard were ever weakened — e.g. /setup/start added to
    # PUBLIC_PATHS — this request would instead write a settings version and
    # this assertion on `service.snapshot()` would catch it.
    from fastapi.testclient import TestClient
    monkeypatch.setenv("JOB_AGG_TAILORED_DIR", str(tmp_path / "tailored"))
    service = make_service()
    app = create_app(stores=sqlite_stores(connect(":memory:")), service=service)
    r = TestClient(app, follow_redirects=False).post("/setup/start")
    assert r.status_code == 401
    assert service.snapshot() is None


def test_setup_offers_restore_from_a_backup(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch, make_service())
    r = c.get("/setup")
    assert "/setup/restore" in r.text
    assert 'enctype="multipart/form-data"' in r.text


def test_restoring_on_a_fresh_install_configures_it(tmp_path, monkeypatch):
    from src.settings.archive import export_zip
    blob = export_zip(make_service({"filters": {"titles": ["staff engineer"]}}),
                      templates_dir=tmp_path / "none")
    c = _client(tmp_path, monkeypatch, make_service())
    r = c.post("/setup/restore", files={"archive": ("b.zip", blob, "application/zip")})
    assert r.status_code == 303
    assert r.headers["location"] == "/settings/overview"
    assert c.app.state.service.snapshot().cfg.filters.titles == ["staff engineer"]


def test_restore_is_rejected_once_the_instance_is_set_up(tmp_path, monkeypatch):
    """/setup/restore is the fresh-install path; a configured instance must
    use /settings/backup, which runs the import guard."""
    c = _client(tmp_path, monkeypatch, make_service({"filters": {"titles": ["keep me"]}}))
    empty_zip = b"PK\x05\x06" + b"\0" * 18
    r = c.post("/setup/restore", files={"archive": ("b.zip", empty_zip, "application/zip")})
    assert r.status_code == 303
    assert r.headers["location"] == "/settings/backup"
    assert c.app.state.service.snapshot().cfg.filters.titles == ["keep me"]


def test_a_bad_archive_on_setup_reports_and_leaves_it_unconfigured(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch, make_service())
    r = c.post("/setup/restore", files={"archive": ("b.zip", b"nope", "application/zip")})
    assert r.status_code == 400
    assert "zip" in r.text.lower()
    assert c.app.state.service.snapshot() is None


def test_setup_restore_ignores_an_absolute_legacy_path(tmp_path, monkeypatch):
    """Same R11 guard as /settings/backup/import
    (test_an_absolute_legacy_path_key_is_ignored_not_read in
    tests/web/settings/test_backup.py), reached through the fresh-install
    route instead: /setup/restore is an upload too, so a config.yaml smuggled
    inside the archive that points relevance.profile_path at a file on the
    server must be ignored, not read into the profile document, even though
    this instance has never been configured before. If /setup/restore ever
    called import_dir directly with trust_paths defaulting to True instead of
    going through the shared restore_from_upload helper, this would read the
    secret file's contents into the profile document."""
    secret = tmp_path / "outside-the-archive-secret.txt"
    secret.write_text("TOP-SECRET-SERVER-FILE-CONTENTS")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(
            "config.yaml",
            f"filters:\n  titles: [staff engineer]\n"
            f"relevance:\n  enabled: true\n  profile_path: {secret}\n",
        )
    c = _client(tmp_path, monkeypatch, make_service())
    r = c.post("/setup/restore", files={"archive": ("b.zip", buf.getvalue(), "application/zip")})
    assert r.status_code == 303
    assert r.headers["location"] == "/settings/overview"
    assert c.app.state.service.documents().profile is None
    assert c.app.state.service.snapshot().cfg.filters.titles == ["staff engineer"]


def test_setup_restore_guard_refusal_points_at_settings_backup(tmp_path, monkeypatch):
    """Fix round 1 (post-review): /setup/restore's own idempotence check
    (request.state.snapshot read once, at the top of the request) does NOT
    guarantee no settings version exists by the time import_dir's own guard
    runs, moments later, after this (possibly slow) upload has been read and
    extracted off the event loop. A real settings change landing in that
    window — another tab finishing /setup/start, then editing real filters or
    companies, while this upload is still in flight — can raise
    ImportGuardRefused here even though the instance looked unconfigured when
    this request started.

    Simulates that race directly (the TestClient is synchronous, so no two
    real requests can interleave): monkeypatches
    src.web.settings.backup.import_dir so that, right where the real
    import_dir would run inside restore_from_upload's threadpool call, a
    'racing' tab's real change lands first, then delegates to the real
    import_dir. The resulting ImportGuardRefused message must NOT reuse
    /settings/backup/import's "Tick overwrite and upload again." — /setup's
    page has no such checkbox — but instead point at Settings -> Backup,
    which does have one. (Settings -> Backup is also exactly where the
    operator ends up if they retry /setup/restore now: the idempotence check
    would redirect them there anyway, since a settings version exists.)"""
    import src.web.settings.backup as backup_mod
    from src.settings.archive import export_zip

    service = make_service()  # unconfigured when this request starts
    c = _client(tmp_path, monkeypatch, service)

    real_import_dir = backup_mod.import_dir

    def racing_import_dir(svc, directory, **kwargs):
        svc.save_settings({}, source="ui", note="started from defaults")
        svc.save_settings({"filters": {"titles": ["real change"]}}, source="ui", note="raced")
        return real_import_dir(svc, directory, **kwargs)

    monkeypatch.setattr(backup_mod, "import_dir", racing_import_dir)

    blob = export_zip(make_service({"filters": {"titles": ["staff engineer"]}}),
                      templates_dir=tmp_path / "none")
    r = c.post("/setup/restore", files={"archive": ("b.zip", blob, "application/zip")})
    assert r.status_code == 409
    assert "Settings" in r.text and "Backup" in r.text
    assert "Tick overwrite and upload again." not in r.text
    # Nothing from THIS request was written — the racing change (simulating
    # the other tab) is what's in effect.
    assert service.snapshot().cfg.filters.titles == ["real change"]
