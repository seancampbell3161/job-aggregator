"""A settings backup as a single ZIP.

Export reuses transfer.export_dir into a temp directory and zips the result,
so the archive layout and the directory layout can never drift apart.

Import is the dangerous direction: an uploaded archive is untrusted input
written to disk, and transfer.import_dir then copies template packs into
JOB_AGG_TEMPLATES_DIR. So extract_upload REJECTS rather than sanitizes -- a
surprising member means a bad archive, and silently "fixing" it would write
something the user did not send.

Every member is checked -- name shape, path, symlink/device, encryption,
compression method, per-member compression ratio -- and the archive's
declared member count and total uncompressed size are checked, all before a
single byte is written. Those size checks read the ZIP's own header fields
(ZipInfo.file_size / compress_size). That is not merely a fast pre-check:
zipfile.ZipExtFile itself treats a member's declared file_size as a hard
ceiling on how many decompressed bytes .read() will ever return for it (see
_read1's `data = data[:self._left]`), and compress_size likewise bounds how
many compressed bytes are consumed -- so a member can never actually produce
more output than its own header claims, and a header-based cap is a complete
guard against a maliciously undersized compressed payload expanding past it,
not just a best-effort one.

Not everything zipfile can throw is predictable from the header, though: a
corrupt CRC-32, or a compress_size that overruns into the next entry's data
("Overlapped entries"), only surface when a member is actually opened and
read. The write pass is wrapped so any such failure -- or an OS-level one,
like a name too long for the filesystem -- is also an ArchiveRejected with
nothing left behind, not an unhandled exception with a partial tree on
disk."""
from __future__ import annotations

import io
import shutil
import stat
import tempfile
import zipfile
import zlib
from pathlib import Path

from src.settings.service import ConfigService
from src.settings.transfer import export_dir

MAX_MEMBERS = 500
MAX_TOTAL_BYTES = 64 * 1024 * 1024   # uncompressed, across the whole archive
MAX_RATIO = 200                      # per member, uncompressed / compressed
MAX_NAME_LENGTH = 255                # a single member name; guards ENAMETOOLONG

# Errors zipfile (or the filesystem) can raise only once a member is actually
# opened and read -- a corrupt CRC, a compress_size that overruns into the
# next entry, an unsupported feature the header check below didn't already
# name, or an OS-level failure such as a path too long for the filesystem.
_EXTRACTION_ERRORS = (OSError, zipfile.BadZipFile, zlib.error, NotImplementedError, RuntimeError, ValueError)


class ArchiveRejected(Exception):
    """The uploaded archive is not a settings backup we will extract."""


def export_zip(service: ConfigService, *, templates_dir: Path | str | None = None) -> bytes:
    """A settings backup (config, documents, user template packs) as one zip."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "settings"
        export_dir(service, root, templates_dir=templates_dir)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(root.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(root).as_posix())
        return buf.getvalue()


def extract_upload(data: bytes, dest: Path | str) -> None:
    """Extract an uploaded backup into ``dest``.

    Raises ArchiveRejected, having written nothing, for anything that is not
    a plain tree of regular files within the declared caps. Every member is
    validated in one pass before a second pass writes anything, so a
    rejection from that first pass never leaves a partial extraction behind
    -- and the second pass is itself wrapped so a failure zipfile only
    raises while actually reading a member (see module docstring) cleans up
    after itself the same way, instead of leaking a partial tree plus a
    non-ArchiveRejected exception."""
    dest = Path(dest)
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ArchiveRejected(f"not a zip file: {exc}") from None

    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_MEMBERS:
            raise ArchiveRejected(
                f"too many files in the archive ({len(infos)}, limit {MAX_MEMBERS})")

        total = 0
        has_file = False
        for info in infos:
            _check_member(info)
            total += info.file_size
            if total > MAX_TOTAL_BYTES:
                raise ArchiveRejected(
                    f"archive expands to at least {total // (1024 * 1024)} MB, "
                    f"more than the {MAX_TOTAL_BYTES // (1024 * 1024)} MB limit")
            has_file = has_file or not info.is_dir()
        if not has_file:
            raise ArchiveRejected("archive has no files to restore")

        # Every member has been checked before anything is written.
        created_dest = not dest.exists()
        dest.mkdir(parents=True, exist_ok=True)
        root = dest.resolve()
        touched: list[Path] = []
        try:
            for info in infos:
                if info.is_dir():
                    continue
                target = (root / info.filename).resolve()
                if not target.is_relative_to(root):   # belt and braces after _check_member
                    raise ArchiveRejected(f"{info.filename}: escapes the destination")
                target.parent.mkdir(parents=True, exist_ok=True)
                # Recorded before opening/writing: open(target, "wb") alone
                # creates a 0-byte file, and a failure can come from reading
                # the member (e.g. a bad CRC-32, raised by src.read() as an
                # argument to out.write()) before a single byte lands -- that
                # half-written file must still be cleaned up on failure.
                touched.append(target)
                with archive.open(info) as src, open(target, "wb") as out:
                    out.write(src.read())
        except ArchiveRejected:
            _cleanup(dest, created_dest, touched)
            raise
        except _EXTRACTION_ERRORS as exc:
            _cleanup(dest, created_dest, touched)
            raise ArchiveRejected(f"{info.filename}: could not be extracted ({exc})") from exc


def _cleanup(dest: Path, created_dest: bool, touched: list[Path]) -> None:
    """Undo a partial extraction. If this call created ``dest``, everything
    under it is ours to remove; otherwise remove only the files this call
    itself touched, leaving whatever ``dest`` already held untouched."""
    if created_dest:
        shutil.rmtree(dest, ignore_errors=True)
    else:
        for path in touched:
            path.unlink(missing_ok=True)


def _check_member(info: zipfile.ZipInfo) -> None:
    name = info.filename
    if len(name) > MAX_NAME_LENGTH:
        raise ArchiveRejected(
            f"{name[:80]}...: member name is {len(name)} characters, "
            f"longer than the {MAX_NAME_LENGTH}-character limit")
    if "\x00" in name:
        raise ArchiveRejected(f"{name!r}: a NUL byte is not allowed in a member name")
    if "\\" in name:
        raise ArchiveRejected(f"{name}: a backslash is not allowed in a member name")
    if name.startswith("/") or (len(name) > 1 and name[1] == ":"):
        raise ArchiveRejected(f"{name}: absolute paths are not allowed")
    parts = Path(name).parts
    if not parts:
        # "", ".", "./" and the like: Path(...).parts is empty, so it does
        # not contain ".." and would sail past the traversal check below.
        # Left unchecked, such a name resolves to the destination directory
        # itself (root / "" == root), so open(target, "wb") raises
        # IsADirectoryError -- an unhandled crash, not a clean rejection.
        raise ArchiveRejected(f"{name!r}: not a usable member name")
    if ".." in parts:
        raise ArchiveRejected(f"{name}: '..' is not allowed")
    # external_attr's upper 16 bits are a Unix mode, but only when the
    # archive carries one at all: zipfile.writestr(str, data) -- the
    # ordinary way to add a member from bytes -- sets permission bits
    # (0o600) and leaves the file-type bits unset, and archives from
    # non-Unix tools may leave the whole field 0. Treat "no type bits" as
    # "unknown, assume regular"; only a type bit that positively says
    # symlink/device/socket/etc. is a rejection.
    file_type = stat.S_IFMT(info.external_attr >> 16)
    if file_type and file_type not in (stat.S_IFREG, stat.S_IFDIR):
        raise ArchiveRejected(f"{name}: not a regular file (symlink or device)")
    if info.flag_bits & 0x1:
        raise ArchiveRejected(f"{name}: encrypted members are not allowed")
    if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
        raise ArchiveRejected(f"{name}: unsupported compression method ({info.compress_type})")
    if info.compress_size:
        ratio = info.file_size / info.compress_size
        if ratio > MAX_RATIO:
            raise ArchiveRejected(
                f"{name}: compression ratio {ratio:.0f}x exceeds the {MAX_RATIO}x "
                "limit -- looks like a zip bomb")
