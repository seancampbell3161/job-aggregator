"""The archive layer: a settings backup as one file, and a hostile one refused."""
import builtins
import errno
import io
import struct
import zipfile
from pathlib import Path

import pytest

from src.settings.archive import (
    MAX_MEMBERS, MAX_NAME_LENGTH, MAX_RATIO, MAX_TOTAL_BYTES,
    ArchiveRejected, _check_member, export_zip, extract_upload,
)
from src.settings.transfer import import_dir
from tests.settings_helpers import make_service


def _zip(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, body in members.items():
            z.writestr(name, body)
    return buf.getvalue()


def _patch_central_dir(blob: bytes, filename: str, field_offset: int, new_value: int, size: int) -> bytes:
    """Overwrite a little-endian integer field in the central-directory
    record for ``filename``, to build archives whose header lies about a
    field zipfile itself does not validate until it actually opens the
    member (CRC-32, compress_size) -- something no ordinary writestr() call
    can produce, since zipfile computes those honestly. Field offsets are
    from the PK\\x01\\x02 signature: CRC-32 at 16, compress_size at 20 (both
    4 bytes), per the ZIP central-directory-record layout."""
    name_bytes = filename.encode()
    sig = b"PK\x01\x02"
    idx = 0
    while True:
        idx = blob.index(sig, idx)
        (name_len,) = struct.unpack("<H", blob[idx + 28:idx + 30])
        if blob[idx + 46: idx + 46 + name_len] == name_bytes:
            pos = idx + field_offset
            return blob[:pos] + new_value.to_bytes(size, "little") + blob[pos + size:]
        idx += 1


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


def test_a_backslash_in_a_member_name_is_refused(tmp_path):
    # On POSIX this is contained (backslash isn't a separator, so it can
    # only ever produce one literal, oddly-named file under root) but it is
    # still rejected: on Windows a name like this would only be caught by
    # the belt-and-braces check in the write pass, after dest.mkdir() and
    # after any earlier legitimate members were already written.
    with pytest.raises(ArchiveRejected, match=r"backslash"):
        extract_upload(_zip({"..\\..\\escape.yaml": b"x"}), tmp_path / "out")
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
    # through. Left unchecked, such a name resolves to the destination
    # directory itself (root / "" == root), and open(target, "wb") raises
    # IsADirectoryError -- an unhandled crash, not a clean rejection.
    # (Verified directly: with the "not parts" guard removed, this archive
    # makes extract_upload raise a bare IsADirectoryError, not ArchiveRejected.)
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


def test_the_total_bytes_cap_is_read_from_the_header_not_the_output(tmp_path, monkeypatch):
    """Proves the cap is enforced from the archive's own header, cheaply: a
    tiny, fast-to-build archive whose declared size trips a monkeypatched-
    down cap. If the check were done by measuring bytes actually written,
    this archive (a few real bytes) would sail through; it is rejected only
    because the cap is read from the member's declared file_size in the zip
    header, before extraction. The message also names the observed total so
    a legitimate archive that trips the real cap is diagnosable."""
    monkeypatch.setattr("src.settings.archive.MAX_TOTAL_BYTES", 10)
    blob = _zip({"small.txt": b"x" * 11})
    with pytest.raises(ArchiveRejected, match=r"at least 0 MB.*more than the 0 MB limit"):
        extract_upload(blob, tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_a_high_ratio_member_is_refused_even_under_the_total_cap(tmp_path):
    """Isolates MAX_RATIO from MAX_TOTAL_BYTES: a highly compressible member
    whose declared uncompressed size is comfortably under the total-bytes
    cap, but whose ratio alone exceeds MAX_RATIO. A total-bytes-only check
    would let this through. The message also names the observed ratio."""
    payload = b"0" * (2 * 1024 * 1024)  # 2 MB of zeroes; well under MAX_TOTAL_BYTES
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("comp.txt", payload)
    blob = buf.getvalue()
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        info = z.getinfo("comp.txt")
        observed_ratio = info.file_size / info.compress_size
        assert observed_ratio > MAX_RATIO, "fixture must exceed MAX_RATIO"
        assert info.file_size <= MAX_TOTAL_BYTES
    # Uses the SAME format spec as the implementation (f"{ratio:.0f}") rather
    # than a separately-written int(observed_ratio): a truncating int() and
    # a rounding :.0f can disagree on a x.5-ish value, so building the
    # expectation from int() would only agree with the implementation by
    # luck. Formatting both sides identically makes them agree by
    # construction instead.
    with pytest.raises(ArchiveRejected, match=rf"ratio {observed_ratio:.0f}x exceeds the {MAX_RATIO}x"):
        extract_upload(blob, tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_an_encrypted_member_is_refused_and_nothing_is_written(tmp_path):
    """zipfile does not preserve a manually-set encryption flag through its
    own writer (it gets reset), so this patches the central-directory flag
    bits directly -- the same technique a real hostile archive would use.
    A benign member comes first, to prove a rejection on the second member
    unwinds the first one too, not just leaves it un-added."""
    blob = _zip({"config.yaml": b"filters: {}\n", "secret.txt": b"x"})
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        flags = z.getinfo("secret.txt").flag_bits
    blob = _patch_central_dir(blob, "secret.txt", 8, flags | 0x1, 2)
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        assert z.getinfo("secret.txt").flag_bits & 0x1, "fixture must be flagged encrypted"
    # The specific phrasing matters here, not just "ArchiveRejected": zipfile
    # itself would also raise on this (RuntimeError, "... is encrypted,
    # password required ..."), which the write-loop's except clause below
    # also converts to ArchiveRejected -- and that fallback message also
    # contains the word "encrypted". Matching the fuller phrase proves THIS
    # check (the pass-one one, which rejects before anything is written) is
    # what actually fired, not just that some layer eventually did.
    with pytest.raises(ArchiveRejected, match="encrypted members are not allowed"):
        extract_upload(blob, tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_an_unsupported_compression_method_is_refused_and_nothing_is_written(tmp_path):
    """Compression method 99 is WinZip AES; zipfile's own write path refuses
    to write it directly (NotImplementedError at write time), so this
    patches the central directory after writing normally -- again, the same
    thing a hostile archive built by another tool could do on its own."""
    blob = _zip({"config.yaml": b"filters: {}\n", "aes.bin": b"x"})
    blob = _patch_central_dir(blob, "aes.bin", 10, 99, 2)
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        assert z.getinfo("aes.bin").compress_type == 99, "fixture must record method 99"
    # As above: zipfile's own fallback (NotImplementedError, "That
    # compression method is not supported"), caught by the write-loop's
    # except clause, would also read as "compression method" -- matching the
    # method number, which only the pass-one message includes, proves this
    # check (not the fallback) is what fired.
    with pytest.raises(ArchiveRejected, match=r"unsupported compression method \(99\)"):
        extract_upload(blob, tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_a_bad_crc_member_is_refused_and_nothing_is_written(tmp_path):
    """A corrupt CRC-32 is invisible to header-only checks (MAX_RATIO and
    MAX_TOTAL_BYTES don't touch CRC) -- zipfile only raises BadZipFile once
    the member is actually opened and read to EOF. Proves the write-loop's
    except clause converts that into a clean ArchiveRejected with cleanup,
    not a leaked zipfile.BadZipFile and a 0-byte file left behind."""
    blob = _zip({"config.yaml": b"filters: {}\n", "bad.txt": b"hello world"})
    blob = _patch_central_dir(blob, "bad.txt", 16, 0xDEADBEEF, 4)
    with pytest.raises(ArchiveRejected, match="bad.txt"):
        extract_upload(blob, tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_an_overrunning_compress_size_is_refused_and_nothing_is_written(tmp_path):
    """A compress_size big enough to run into the next entry's data is also
    invisible to header-only size/ratio checks (it would make the ratio
    look SMALLER, not bigger) -- zipfile only raises "Overlapped entries"
    once the member is actually opened.

    Re-derived rather than assumed (a prior version of this docstring
    wrongly claimed "the earlier hostile write still gets fully unwound" --
    there is no write to unwind: zipfile.ZipFile.open() raises "Overlapped
    entries" before the target is ever created, since `archive.open(info)`
    is evaluated and entered before `open(target, "wb")` in `with
    archive.open(info) as src, open(target, "wb") as out:`. bad.txt is
    listed first here only so it fails before config.yaml -- a member later
    in iteration order -- is ever reached at all; what actually proves the
    "nothing left behind" invariant here is dest.mkdir() being unwound by
    _cleanup's created_root branch, not any per-file cleanup."""
    blob = _zip({"bad.txt": b"hello world", "config.yaml": b"filters: {}\n"})
    blob = _patch_central_dir(blob, "bad.txt", 20, len(blob), 4)
    with pytest.raises(ArchiveRejected, match="bad.txt"):
        extract_upload(blob, tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_an_overlong_member_name_is_refused_and_nothing_is_written(tmp_path):
    blob = _zip({"config.yaml": b"filters: {}\n", "f" * 5000 + ".txt": b"x"})
    with pytest.raises(ArchiveRejected, match=str(MAX_NAME_LENGTH)):
        extract_upload(blob, tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_a_nul_byte_in_a_member_name_is_rejected_by_check_member():
    """CPython's zipfile can never actually hand extract_upload a filename
    containing a NUL: ZipInfo.__init__ unconditionally truncates at the
    first NUL byte (zipfile._sanitize_filename, "Null bytes in file names
    are used as tricks by viruses in archives"), and
    ZipFile._RealGetContents constructs every parsed central-directory
    entry via `ZipInfo(filename)` -- so this is applied to EVERY archive
    zipfile parses, however it was built, before any of our code runs.
    Confirmed empirically: neither zipfile.ZipInfo("a\\x00b") nor writing
    with a post-construction `info.filename = "a\\x00b"` (bypassing
    __init__) survives a round trip -- both come back as "a" from
    zipfile.ZipFile(...).infolist(). So there is no archive bytes-level
    input that reaches _check_member with a NUL still in info.filename;
    this check is unreachable through extract_upload's public API and is
    kept only as a defensive backstop (per review), tested directly against
    _check_member with a ZipInfo whose .filename is force-set post
    construction, since that is the only way to exercise this line at all."""
    info = zipfile.ZipInfo("placeholder.txt")
    info.filename = "bad\x00name.txt"    # bypasses __init__'s sanitizer directly
    with pytest.raises(ArchiveRejected, match="NUL"):
        _check_member(info)


def test_an_archive_with_no_files_is_refused(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("resume/templates/mine/", b"")
    with pytest.raises(ArchiveRejected, match="no files"):
        extract_upload(buf.getvalue(), tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_a_truly_empty_archive_is_refused(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w"):
        pass
    with pytest.raises(ArchiveRejected, match="no files"):
        extract_upload(buf.getvalue(), tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_a_file_that_is_not_a_zip_is_refused(tmp_path):
    with pytest.raises(ArchiveRejected, match="not a .?zip"):
        extract_upload(b"this is a text file", tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_a_rejection_names_the_member(tmp_path):
    with pytest.raises(ArchiveRejected) as exc:
        extract_upload(_zip({"../escape.yaml": b"x"}), tmp_path / "out")
    assert "escape.yaml" in str(exc.value)


def test_a_valid_archive_extracts_the_expected_files(tmp_path):
    blob = _zip({"config.yaml": b"filters: {}\n", "resume/templates/mine/template.html.j2": b"x"})
    extract_upload(blob, tmp_path / "out")
    assert (tmp_path / "out" / "config.yaml").read_bytes() == b"filters: {}\n"
    assert (tmp_path / "out" / "resume" / "templates" / "mine" / "template.html.j2").read_bytes() == b"x"


def test_extracting_into_an_existing_directory_only_removes_what_this_call_wrote(tmp_path):
    """When dest already existed before the call (so extract_upload did not
    create it), a failure must not delete content that was already there --
    only the files this call itself wrote. Uses a write-loop failure (bad
    CRC), not a pass-one rejection, because pass one never writes anything
    at all -- this specifically exercises the cleanup path in the except
    clause around the write loop, with a benign member that really does get
    written to the pre-existing directory before the bad one fails.

    Note this fixture's pre-existing file (keep.txt) is NOT itself an
    archive member -- see
    test_a_pre_existing_member_named_file_survives_a_rejected_archive below
    for that case, which is the one that actually broke this invariant
    (round 2 finding "Important A")."""
    dest = tmp_path / "out"
    dest.mkdir()
    (dest / "keep.txt").write_text("pre-existing")
    blob = _zip({"config.yaml": b"filters: {}\n", "bad.txt": b"hello world"})
    blob = _patch_central_dir(blob, "bad.txt", 16, 0xDEADBEEF, 4)
    with pytest.raises(ArchiveRejected):
        extract_upload(blob, dest)
    assert (dest / "keep.txt").read_text() == "pre-existing"
    assert not (dest / "config.yaml").exists()
    assert not (dest / "bad.txt").exists()


def test_a_pre_existing_member_named_file_survives_a_rejected_archive(tmp_path):
    """Round 2, Important A: _cleanup deleted pre-existing content whose
    name happened to collide with an archive member, because the target was
    queued for removal (`touched.append(target)`) BEFORE `archive.open()`
    ever ran -- and here `archive.open()` itself raises ("Overlapped
    entries", from an inflated compress_size), so the target file is never
    opened, truncated, or written at all. The bug was invisible in the test
    above because its pre-existing file was never a member name the
    archive also used -- this one specifically is, which is the case that
    breaks the invariant."""
    blob = _zip({"keep.txt": b"hello world", "config.yaml": b"filters: {}\n"})
    blob = _patch_central_dir(blob, "keep.txt", 20, len(blob), 4)
    dest = tmp_path / "out"
    dest.mkdir()
    (dest / "keep.txt").write_text("USER CONTENT DO NOT DELETE")
    with pytest.raises(ArchiveRejected, match="keep.txt"):
        extract_upload(blob, dest)
    assert (dest / "keep.txt").read_text() == "USER CONTENT DO NOT DELETE"
    assert not (dest / "config.yaml").exists()


def test_a_pre_existing_directory_colliding_with_a_member_name_is_left_alone(tmp_path):
    """Round 2, Important B: a pre-existing directory ("sub/") whose name
    collides with an archive member also named "sub" makes
    open(target, "wb") raise IsADirectoryError, which _EXTRACTION_ERRORS
    correctly converts to ArchiveRejected -- but the OLD _cleanup then tried
    path.unlink() on that same still-existing directory and raised
    PermissionError/IsADirectoryError out of the except block, so
    ArchiveRejected never actually reached the caller. Proves both that a
    clean ArchiveRejected is raised AND that the pre-existing directory and
    its contents survive."""
    blob = _zip({"sub": b"x", "config.yaml": b"filters: {}\n"})
    dest = tmp_path / "out"
    (dest / "sub").mkdir(parents=True)
    (dest / "sub" / "userfile.txt").write_text("user data")
    with pytest.raises(ArchiveRejected, match="sub"):
        extract_upload(blob, dest)
    assert (dest / "sub").is_dir()
    assert (dest / "sub" / "userfile.txt").read_text() == "user data"
    assert not (dest / "config.yaml").exists()


def test_a_colliding_pre_existing_file_is_left_truncated_by_a_bad_crc_member(tmp_path):
    """Round 3 (reviewer's probe P1): pins the DOCUMENTED behaviour, not an
    aspirational one. _cleanup never DELETES a pre-existing path (the two
    tests above), but open(target, "wb") truncates it the instant it is
    opened -- before the member is even read -- so a bad-CRC member (unlike
    the Overlapped-entries fixture above, which fails inside archive.open()
    before the target is ever opened at all) leaves a colliding pre-existing
    file truncated to empty, not restored to its original content. This is
    exactly what extract_upload's docstring now says can happen when dest is
    not the fresh, empty directory it is meant to be; a test that avoided
    this shape (as the Important-A regression test above does, deliberately,
    to isolate that finding) would leave the docstring's claim unverified."""
    blob = _zip({"keep.txt": b"hello world"})
    blob = _patch_central_dir(blob, "keep.txt", 16, 0xDEADBEEF, 4)  # bad CRC
    dest = tmp_path / "out"
    dest.mkdir()
    (dest / "keep.txt").write_text("USER CONTENT DO NOT DELETE")
    with pytest.raises(ArchiveRejected, match="keep.txt"):
        extract_upload(blob, dest)
    assert (dest / "keep.txt").exists()            # cleanup never deletes it --
    assert (dest / "keep.txt").read_text() == ""   # -- but it is left truncated, not restored


def test_a_colliding_pre_existing_file_is_overwritten_when_a_later_member_fails(tmp_path):
    """Round 3 (reviewer's probe P2): the other documented shape. A
    colliding member writes successfully in full (so the pre-existing file
    is completely overwritten with the archive's content), then a LATER
    member fails. The whole restore is still reported as ArchiveRejected,
    but the successful collision is not rolled back: the user's original
    content is gone, replaced by the archive's (also-rejected) content."""
    blob = _zip({"config.yaml": b"ARCHIVE CONTENT", "bad.txt": b"hello world"})
    blob = _patch_central_dir(blob, "bad.txt", 16, 0xDEADBEEF, 4)  # bad CRC, fails later
    dest = tmp_path / "out"
    dest.mkdir()
    (dest / "config.yaml").write_text("USER'S ORIGINAL CONFIG")
    with pytest.raises(ArchiveRejected):
        extract_upload(blob, dest)
    assert (dest / "config.yaml").read_text() == "ARCHIVE CONTENT"   # overwritten, not restored


def test_cleanup_removes_sibling_new_directories_it_created(tmp_path):
    """A failure partway through must remove every directory THIS call
    created, not just a single linear chain: two independent new
    subdirectories (a/b and a/c, sharing new parent a) plus a pre-existing
    file are set up in an already-existing dest, then a later member fails.
    A cleanup that only walks one parent chain, or removes shallow-to-deep
    instead of deep-to-first, would leave "a" (now-empty but attempted
    first) behind."""
    blob = _zip({"a/b/x.txt": b"x", "a/c/y.txt": b"y", "bad.txt": b"hello world"})
    blob = _patch_central_dir(blob, "bad.txt", 16, 0xDEADBEEF, 4)
    dest = tmp_path / "out"
    dest.mkdir()
    (dest / "keep.txt").write_text("keep me")
    with pytest.raises(ArchiveRejected):
        extract_upload(blob, dest)
    remaining = sorted(str(p.relative_to(dest)) for p in dest.rglob("*"))
    assert remaining == ["keep.txt"]


def test_cleanup_removes_dests_own_new_parent_directories_too(tmp_path):
    """dest.mkdir(parents=True) can create levels ABOVE dest itself when
    none of dest's ancestors exist yet -- if cleanup only removes dest (not
    also the new parents mkdir(parents=True) created for it), those
    intermediates leak. Neither tmp_path/new_a nor tmp_path/new_a/new_b
    (dest's parent) exist beforehand; a WRITE-LOOP failure (not a pass-one
    rejection, which never calls dest.mkdir() at all and so would prove
    nothing here) must remove the whole new_a/new_b/out chain it created,
    not just dest itself."""
    top = tmp_path / "new_a"
    dest = top / "new_b" / "out"
    assert not top.exists()
    blob = _zip({"bad.txt": b"hello world"})
    blob = _patch_central_dir(blob, "bad.txt", 16, 0xDEADBEEF, 4)
    with pytest.raises(ArchiveRejected):
        extract_upload(blob, dest)
    assert not top.exists()


def test_a_legitimately_deep_export_path_over_255_chars_total_is_not_rejected(tmp_path):
    """Round 2 finding: MAX_NAME_LENGTH must be a per-component (NAME_MAX)
    limit, not a whole-path one -- PATH_MAX covers the whole path, and
    depends on dest's own absolute path length, which the archive doesn't
    control. This path is 268 characters in total (over the old whole-path
    255 limit) but every component is comfortably under 255 bytes, matching
    a real, legitimate resume/templates/<pack>/<file> export."""
    name = "resume/templates/" + "d" * 150 + "/fonts/" + "f" * 90 + ".ttf"
    assert len(name) > MAX_NAME_LENGTH
    assert all(len(part.encode("utf-8")) < MAX_NAME_LENGTH for part in name.split("/"))
    blob = _zip({name: b"x", "config.yaml": b"filters: {}\n"})
    extract_upload(blob, tmp_path / "out")
    assert (tmp_path / "out" / name).read_bytes() == b"x"


def test_an_unsupported_zip_version_is_refused(tmp_path):
    """"Version needed to extract" beyond what zipfile supports raises
    NotImplementedError from ZipFile.__init__ itself (_RealGetContents),
    before extract_upload's own per-member checks ever run -- the original
    except clause around that constructor call only caught BadZipFile."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("config.yaml", b"filters: {}\n")
    blob = _patch_central_dir(buf.getvalue(), "config.yaml", 6, 100, 2)  # extract_version
    with pytest.raises(ArchiveRejected, match="unsupported zip feature"):
        extract_upload(blob, tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_an_environment_failure_is_not_reported_as_a_bad_archive(tmp_path, monkeypatch):
    """A genuine server-side fault (disk full, permission denied) writing
    to dest is not the upload's fault, and must not be reported as one --
    it should propagate as a plain OSError so a caller can tell "your
    archive is invalid" apart from "the server broke", rather than folding
    both into ArchiveRejected."""
    real_open = builtins.open

    def flaky_open(path, mode="r", *args, **kwargs):
        if mode == "wb" and str(path).endswith("config.yaml"):
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", flaky_open)
    blob = _zip({"config.yaml": b"filters: {}\n"})
    with pytest.raises(OSError) as exc:
        extract_upload(blob, tmp_path / "out")
    assert not isinstance(exc.value, ArchiveRejected)
    assert exc.value.errno == errno.ENOSPC
    assert not (tmp_path / "out").exists()


def test_cleanups_own_unlink_failure_does_not_mask_the_original_rejection(tmp_path, monkeypatch):
    """archive.py's _cleanup: the touched-file unlink loop was unguarded
    while the adjacent rmdir loop was already wrapped in try/except OSError
    -- the exact shape of the defect round 2 fixed (cleanup itself raising
    and swallowing the real ArchiveRejected). No real archive can trigger
    this (the reviewer could not construct one either -- missing_ok=True
    already handles "already gone"), so this monkeypatches Path.unlink to
    simulate the kind of environment failure (permission revoked mid-
    cleanup) that would. Needs a pre-existing dest so _cleanup takes the
    per-file unlink branch rather than a whole-directory rmtree."""
    dest = tmp_path / "out"
    dest.mkdir()
    (dest / "keep.txt").write_text("pre-existing, unrelated")
    blob = _zip({"bad.txt": b"hello world"})
    blob = _patch_central_dir(blob, "bad.txt", 16, 0xDEADBEEF, 4)

    real_unlink = Path.unlink

    def flaky_unlink(self, missing_ok=False):
        if self.name == "bad.txt":
            raise PermissionError(errno.EACCES, "permission denied")
        return real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)
    with pytest.raises(ArchiveRejected, match="bad.txt"):
        extract_upload(blob, dest)
    assert (dest / "keep.txt").read_text() == "pre-existing, unrelated"


def test_a_partial_dest_creation_failure_cleans_up_what_it_made(tmp_path, monkeypatch):
    """archive.py: _ensure_dir(dest, dest_created) used to run outside the
    try block, so if mkdir() failed partway up a chain of new parent
    directories, whatever was already created leaked with no cleanup.
    Monkeypatches Path.mkdir to fail on the second (deeper) of two new
    levels, after the first has already been created."""
    top = tmp_path / "new_a"
    dest = top / "new_b"
    real_mkdir = Path.mkdir

    def flaky_mkdir(self, *args, **kwargs):
        if self == dest:
            raise PermissionError(errno.EACCES, "permission denied")
        return real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", flaky_mkdir)
    blob = _zip({"config.yaml": b"filters: {}\n"})
    with pytest.raises(PermissionError):
        extract_upload(blob, dest)
    assert not top.exists()   # "new_a" was created before the failure; must not leak
