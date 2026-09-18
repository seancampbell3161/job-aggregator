"""A settings backup as a single ZIP.

Export reuses transfer.export_dir into a temp directory and zips the result,
so the archive layout and the directory layout can never drift apart.

Import is the dangerous direction: an uploaded archive is untrusted input
written to disk, and transfer.import_dir then copies template packs into
JOB_AGG_TEMPLATES_DIR. So extract_upload REJECTS rather than sanitizes -- a
surprising member means a bad archive, and silently "fixing" it would write
something the user did not send.

Every member is checked -- path, symlink/device, per-member compression
ratio -- and the archive's declared member count and total uncompressed size
are checked, all before a single byte is written. Those checks read the
ZIP's own header fields (ZipInfo.file_size / compress_size). That is not
merely a fast pre-check: zipfile.ZipExtFile itself treats a member's declared
file_size as a hard ceiling on how many decompressed bytes .read() will ever
return for it (see _read1's `data = data[:self._left]`), and compress_size
likewise bounds how many compressed bytes are consumed -- so a member can
never actually produce more output than its own header claims, and a
header-based cap is a complete guard against a maliciously undersized
compressed payload expanding past it, not just a best-effort one."""
from __future__ import annotations

import io
import stat
import tempfile
import zipfile
from pathlib import Path

from src.settings.service import ConfigService
from src.settings.transfer import export_dir

MAX_MEMBERS = 500
MAX_TOTAL_BYTES = 64 * 1024 * 1024   # uncompressed, across the whole archive
MAX_RATIO = 200                      # per member, uncompressed / compressed


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
    rejection never leaves a partial extraction behind."""
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
        for info in infos:
            _check_member(info)
            total += info.file_size
            if total > MAX_TOTAL_BYTES:
                raise ArchiveRejected(
                    f"archive expands to more than {MAX_TOTAL_BYTES // (1024 * 1024)} MB")

        # Every member has been checked before anything is written.
        dest.mkdir(parents=True, exist_ok=True)
        root = dest.resolve()
        for info in infos:
            if info.is_dir():
                continue
            target = (root / info.filename).resolve()
            if not target.is_relative_to(root):     # belt and braces after _check_member
                raise ArchiveRejected(f"{info.filename}: escapes the destination")
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as src, open(target, "wb") as out:
                out.write(src.read())


def _check_member(info: zipfile.ZipInfo) -> None:
    name = info.filename
    if name.startswith("/") or (len(name) > 1 and name[1] == ":"):
        raise ArchiveRejected(f"{name}: absolute paths are not allowed")
    parts = Path(name).parts
    if not parts:
        # "", ".", "./" and the like: Path(...).parts is empty, so it does
        # not contain ".." and would sail past the traversal check below --
        # but it is not a usable member name either. Left unchecked,
        # ZipInfo.is_dir() raises IndexError on "" (it indexes
        # filename[-1]), and a member named "." resolves to the destination
        # directory itself, so writing it raises IsADirectoryError: both are
        # unhandled crashes, not a clean rejection.
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
    if info.compress_size and info.file_size / info.compress_size > MAX_RATIO:
        raise ArchiveRejected(f"{name}: compression ratio looks like a zip bomb")
