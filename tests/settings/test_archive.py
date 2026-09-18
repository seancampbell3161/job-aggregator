"""The archive layer: a settings backup as one file, and a hostile one refused."""
import io
import zipfile
from pathlib import Path

import pytest

from src.settings.archive import (
    MAX_MEMBERS, MAX_RATIO, MAX_TOTAL_BYTES, ArchiveRejected, export_zip, extract_upload,
)
from src.settings.transfer import import_dir
from tests.settings_helpers import make_service


def _zip(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, body in members.items():
            z.writestr(name, body)
    return buf.getvalue()


def test_export_contains_the_config_and_the_documents(tmp_path):
    service = make_service({"filters": {"titles": ["staff engineer"]}},
                           documents={"profile": "# me"})
    with zipfile.ZipFile(io.BytesIO(export_zip(service, templates_dir=tmp_path / "none"))) as z:
        names = set(z.namelist())
        assert "config.yaml" in names and "profile.md" in names
        assert b"staff engineer" in z.read("config.yaml")


def test_export_never_contains_a_secret(tmp_path):
    service = make_service({}, secrets={"ntfy_topic_url": "https://ntfy.sh/hunter2"})
    blob = export_zip(service, templates_dir=tmp_path / "none")
    assert b"hunter2" not in blob


def test_a_round_trip_reimports(tmp_path):
    # Exercises both a config value and a document, plus a secret that must
    # NOT come back -- the point of the round trip is that it restores real
    # settings, not just that files of the right names appear.
    service = make_service(
        {"filters": {"titles": ["staff engineer"]}},
        documents={"profile": "# Jordan Rivera\n"},
        secrets={"ntfy_topic_url": "https://ntfy.sh/hunter2"},
    )
    blob = export_zip(service, templates_dir=tmp_path / "none")
    extract_upload(blob, tmp_path / "in")
    fresh = make_service({})
    import_dir(fresh, tmp_path / "in", templates_dir=tmp_path / "tpl", force=True)
    snap = fresh.snapshot()
    assert snap.cfg.filters.titles == ["staff engineer"]
    assert snap.documents.profile == "# Jordan Rivera\n"
    assert fresh.effective_secret("ntfy_topic_url") == ""


def test_a_traversing_member_is_refused(tmp_path):
    with pytest.raises(ArchiveRejected, match=r"\.\."):
        extract_upload(_zip({"../escape.yaml": b"x"}), tmp_path / "out")
    # Not just the escaped file: the destination itself was never created,
    # which is the only way to be sure NOTHING was written -- checking for
    # one specific file would miss a partial extraction of everything else.
    assert not (tmp_path / "escape.yaml").exists()
    assert not (tmp_path / "out").exists()


def test_an_absolute_member_is_refused(tmp_path):
    with pytest.raises(ArchiveRejected):
        extract_upload(_zip({"/etc/passwd": b"x"}), tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_a_windows_style_absolute_member_is_refused(tmp_path):
    with pytest.raises(ArchiveRejected):
        extract_upload(_zip({"C:\\Windows\\win.ini": b"x"}), tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_a_symlink_member_is_refused(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        info = zipfile.ZipInfo("link")
        info.external_attr = (0xA000 | 0o777) << 16   # S_IFLNK
        z.writestr(info, b"/etc/passwd")
    with pytest.raises(ArchiveRejected, match="symlink|not a regular file"):
        extract_upload(buf.getvalue(), tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_an_empty_member_name_is_refused(tmp_path):
    # A degenerate name ("", ".", "./") has an empty Path(...).parts tuple,
    # which does not contain "..", so the traversal check alone lets it
    # through. Left unchecked, ZipInfo.is_dir() raises IndexError on "" (it
    # indexes filename[-1]) and a member named "." resolves to the
    # destination directory itself, so open(target, "wb") raises
    # IsADirectoryError -- both are unhandled crashes, not a clean rejection.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(zipfile.ZipInfo(""), b"x")
    with pytest.raises(ArchiveRejected):
        extract_upload(buf.getvalue(), tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_a_self_referencing_member_is_refused(tmp_path):
    with pytest.raises(ArchiveRejected):
        extract_upload(_zip({".": b"x"}), tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_too_many_members_is_refused(tmp_path):
    members = {f"f{i}.txt": b"x" for i in range(MAX_MEMBERS + 1)}
    with pytest.raises(ArchiveRejected, match="too many"):
        extract_upload(_zip(members), tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_a_zip_bomb_is_refused_before_it_is_written(tmp_path):
    # A real 200 MB zip bomb is unpleasant to build in a test, so this lowers
    # the cap instead. What matters is that this trips on the archive's own
    # header (ZipInfo.file_size), a few bytes of metadata, BEFORE any member
    # is opened or written -- not that we noticed a big file appear on disk.
    with pytest.raises(ArchiveRejected):
        extract_upload(_zip({"big.txt": b"0" * (200 * 1024 * 1024)}), tmp_path / "out")
    assert not (tmp_path / "out" / "big.txt").exists()
    assert not (tmp_path / "out").exists()


def test_the_total_bytes_cap_is_read_from_the_header_not_the_output(tmp_path, monkeypatch):
    """Same property as the zip-bomb test, proven cheaply: a tiny, fast-to-
    build archive whose declared size trips a monkeypatched-down cap. If the
    check were done by measuring bytes actually written, this archive (a
    few real bytes) would sail through; it is rejected only because the
    cap is read from the member's declared file_size in the zip header."""
    monkeypatch.setattr("src.settings.archive.MAX_TOTAL_BYTES", 10)
    blob = _zip({"small.txt": b"x" * 11})
    with pytest.raises(ArchiveRejected, match="more than"):
        extract_upload(blob, tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_a_high_ratio_member_is_refused_even_under_the_total_cap(tmp_path):
    """Isolates MAX_RATIO from MAX_TOTAL_BYTES: a highly compressible member
    whose declared uncompressed size is comfortably under the total-bytes
    cap, but whose ratio alone exceeds MAX_RATIO. A total-bytes-only check
    would let this through."""
    payload = b"0" * (2 * 1024 * 1024)  # 2 MB of zeroes; well under MAX_TOTAL_BYTES
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("comp.txt", payload)
    blob = buf.getvalue()
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        info = z.getinfo("comp.txt")
        assert info.file_size / info.compress_size > MAX_RATIO, "fixture must exceed MAX_RATIO"
        assert info.file_size <= MAX_TOTAL_BYTES
    with pytest.raises(ArchiveRejected, match="ratio"):
        extract_upload(blob, tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_a_file_that_is_not_a_zip_is_refused(tmp_path):
    with pytest.raises(ArchiveRejected, match="not a .?zip"):
        extract_upload(b"this is a text file", tmp_path / "out")


def test_a_rejection_names_the_member(tmp_path):
    with pytest.raises(ArchiveRejected) as exc:
        extract_upload(_zip({"../escape.yaml": b"x"}), tmp_path / "out")
    assert "escape.yaml" in str(exc.value)


def test_a_valid_archive_extracts_the_expected_files(tmp_path):
    blob = _zip({"config.yaml": b"filters: {}\n", "resume/templates/mine/template.html.j2": b"x"})
    extract_upload(blob, tmp_path / "out")
    assert (tmp_path / "out" / "config.yaml").read_bytes() == b"filters: {}\n"
    assert (tmp_path / "out" / "resume" / "templates" / "mine" / "template.html.j2").read_bytes() == b"x"
