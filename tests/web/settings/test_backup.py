"""Backup: download everything, upload it back."""
import io
import struct
import zipfile

from src.web.app import create_app
from tests.auth_helpers import signed_in_client
from tests.settings_helpers import make_service


def _app(tmp_path, monkeypatch, service=None):
    monkeypatch.setenv("JOB_AGG_SQLITE_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("JOB_AGG_TAILORED_DIR", str(tmp_path / "tailored"))
    monkeypatch.setenv("JOB_AGG_TEMPLATES_DIR", str(tmp_path / "templates"))
    return create_app(service=service if service is not None else make_service({}))


def test_the_page_says_secrets_are_not_included(tmp_path, monkeypatch):
    r = signed_in_client(_app(tmp_path, monkeypatch)).get("/settings/backup")
    assert "secret" in r.text.lower()


def test_export_downloads_a_named_zip(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, make_service({"filters": {"titles": ["staff engineer"]}}))
    r = signed_in_client(app).get("/settings/backup/export")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    assert "attachment" in r.headers["content-disposition"]
    assert ".zip" in r.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        assert b"staff engineer" in z.read("config.yaml")


def test_import_applies_an_uploaded_backup(tmp_path, monkeypatch):
    source = make_service({"filters": {"titles": ["staff engineer"]}})
    from src.settings.archive import export_zip
    blob = export_zip(source, templates_dir=tmp_path / "none")
    app = _app(tmp_path, monkeypatch)
    r = signed_in_client(app).post(
        "/settings/backup/import",
        files={"archive": ("backup.zip", blob, "application/zip")},
        data={"force": "on"})
    assert r.status_code == 200
    assert app.state.service.snapshot().cfg.filters.titles == ["staff engineer"]
    assert "config.yaml" in r.text


def test_a_hostile_archive_is_refused_and_changes_nothing(tmp_path, monkeypatch):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("../escape.yaml", b"x")
    app = _app(tmp_path, monkeypatch, make_service({"filters": {"titles": ["keep me"]}}))
    r = signed_in_client(app).post(
        "/settings/backup/import",
        files={"archive": ("bad.zip", buf.getvalue(), "application/zip")})
    assert r.status_code == 400
    assert "escape.yaml" in r.text
    assert app.state.service.snapshot().cfg.filters.titles == ["keep me"]


def test_a_malformed_central_directory_is_refused_not_a_500(tmp_path, monkeypatch):
    """Whole-branch review, Important 2: a central-directory filename
    flagged UTF-8 (general-purpose bit 0x800) but holding bytes that are
    not valid UTF-8 used to escape zipfile.ZipFile(...) as an unhandled
    UnicodeDecodeError -- before extract_upload's own per-member checks
    ever ran -- reaching this endpoint as a bare 500 instead of the same
    clean 400 every other malformed archive gets. Built deterministically
    by patching two bytes in a valid archive (see
    tests/settings/test_archive.py's identically-built unit test for the
    same fixture), not by fuzzing here."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("config.yaml", b"filters: {}\n")
        z.writestr("bad.txt", b"x")
    blob = buf.getvalue()

    sig = b"PK\x01\x02"
    idx = 0
    name_bytes = b"bad.txt"
    while True:
        idx = blob.index(sig, idx)
        (name_len,) = struct.unpack("<H", blob[idx + 28:idx + 30])
        if blob[idx + 46: idx + 46 + name_len] == name_bytes:
            break
        idx += 1
    invalid_name = bytes([0xFF, 0xFE, 0xFD, 0xFC, 0xFB, 0xFA, 0xF9])
    pos = idx + 46
    blob = blob[:pos] + invalid_name + blob[pos + name_len:]
    flags_pos = idx + 8
    old_flags = struct.unpack("<H", blob[flags_pos:flags_pos + 2])[0]
    blob = blob[:flags_pos] + struct.pack("<H", old_flags | 0x800) + blob[flags_pos + 2:]

    app = _app(tmp_path, monkeypatch, make_service({"filters": {"titles": ["keep me"]}}))
    r = signed_in_client(app).post(
        "/settings/backup/import",
        files={"archive": ("bad.zip", blob, "application/zip")})
    assert r.status_code == 400
    # Nothing left behind: the settings this instance already had are untouched.
    assert app.state.service.snapshot().cfg.filters.titles == ["keep me"]


def test_an_archive_with_no_config_reports_the_missing_file(tmp_path, monkeypatch):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("profile.md", b"# me")
    app = _app(tmp_path, monkeypatch)
    r = signed_in_client(app).post(
        "/settings/backup/import",
        files={"archive": ("bad.zip", buf.getvalue(), "application/zip")})
    assert r.status_code == 400
    assert "config" in r.text.lower()


def test_the_guard_refusal_is_shown_with_the_overwrite_instruction(tmp_path, monkeypatch):
    """A backup taken before an add-source would undo it; the import must
    refuse and say how to proceed, not silently drop the board."""
    from src.settings.archive import export_zip
    from src.settings.sources import append_slug_sources

    service = make_service({"filters": {"titles": ["x"]}})
    blob = export_zip(service, templates_dir=tmp_path / "none")
    # The export above is the 'files'; now change settings in the DB only.
    service.save_settings({"filters": {"titles": ["x"]}}, source="import", note="import: baseline")
    append_slug_sources(service, {"greenhouse": ["acme"]}, label="add-source")
    app = _app(tmp_path, monkeypatch, service)
    r = signed_in_client(app).post(
        "/settings/backup/import",
        files={"archive": ("backup.zip", blob, "application/zip")})
    assert r.status_code == 409
    assert "acme" in r.text
    assert "overwrite" in r.text.lower()
    # Pinned exactly (round-1 fix): /setup/restore's own guard_hint must stay
    # distinct from this one, since /setup's page has no overwrite checkbox
    # to tick — see test_setup_restore_guard_refusal_points_at_settings_backup
    # in tests/web/test_setup_gate.py.
    assert "Tick overwrite and upload again." in r.text
    assert app.state.service.snapshot().cfg.sources.greenhouse == ["acme"]


def test_an_oversize_upload_is_refused(tmp_path, monkeypatch):
    from src.web.settings.backup import MAX_UPLOAD_BYTES
    app = _app(tmp_path, monkeypatch)
    r = signed_in_client(app).post(
        "/settings/backup/import",
        files={"archive": ("big.zip", b"0" * (MAX_UPLOAD_BYTES + 1), "application/zip")})
    assert r.status_code == 413


def test_no_file_selected_is_a_clear_error(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    r = signed_in_client(app).post("/settings/backup/import", data={})
    assert r.status_code == 400
    assert "choose a file" in r.text.lower()


def test_an_absolute_legacy_path_key_is_ignored_not_read(tmp_path, monkeypatch):
    """R11: a config.yaml smuggled inside an uploaded archive can set a
    LEGACY_PATH_KEYS value (e.g. relevance.profile_path) to an absolute path
    on the SERVER's filesystem. Wired through the web endpoint, honouring it
    would be an authenticated arbitrary-file-read: the file's contents would
    be stored and shown back as the profile document. The upload path must
    ignore it (with a warning), not read it -- unlike the CLI, which trusts
    the path because it's the operator's own shell."""
    secret = tmp_path / "outside-the-archive-secret.txt"
    secret.write_text("TOP-SECRET-SERVER-FILE-CONTENTS")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(
            "config.yaml",
            f"filters:\n  titles: [staff engineer]\n"
            f"relevance:\n  enabled: true\n  profile_path: {secret}\n",
        )
    app = _app(tmp_path, monkeypatch)
    r = signed_in_client(app).post(
        "/settings/backup/import",
        files={"archive": ("backup.zip", buf.getvalue(), "application/zip")})
    assert r.status_code == 200
    # Not just "not the secret" -- the path must be ignored outright, not
    # silently read into some OTHER value.
    assert app.state.service.documents().profile is None
    assert "TOP-SECRET-SERVER-FILE-CONTENTS" not in r.text
    # The other half of R11: the ignored key is a visible warning, not a
    # silent, unexplained no-op.
    assert "points outside the archive — ignored" in r.text
    # The setting itself still applies -- only the dangerous path is ignored.
    assert app.state.service.snapshot().cfg.filters.titles == ["staff engineer"]


def test_a_legacy_path_with_a_nul_byte_never_500s(tmp_path, monkeypatch):
    """IMPORTANT (fix round 1): PyYAML decodes a double-quoted \\0 escape
    into a string with an embedded NUL byte. Path.resolve() -- unlike
    .is_file() -- raises ValueError for that, and _confined used to catch
    only OSError, so this exact upload used to 500 instead of hitting any of
    backup_import's four except clauses. It must come back as a normal
    response (a warning, not a crash) -- never 500."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(
            "config.yaml",
            'filters:\n  titles: [staff engineer]\n'
            'relevance:\n  enabled: true\n  profile_path: "a\\0b"\n',
        )
    app = _app(tmp_path, monkeypatch)
    r = signed_in_client(app).post(
        "/settings/backup/import",
        files={"archive": ("backup.zip", buf.getvalue(), "application/zip")})
    assert r.status_code in (200, 400)
    assert app.state.service.documents().profile is None
