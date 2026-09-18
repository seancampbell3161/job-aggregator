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
corrupt CRC-32, a compress_size that overruns into the next entry's data
("Overlapped entries"), or an unsupported zip-version feature, only surface
once zipfile actually opens/reads a member (or, for the version case, opens
the archive itself). The write pass is wrapped so any such failure -- or a
filesystem one, like a path colliding with something already at the
destination -- is also an ArchiveRejected with nothing new left behind, not
an unhandled exception with a partial tree on disk. A genuine environment
fault (disk full, permission denied on the destination) is deliberately
NOT folded into that -- it isn't the upload's fault, so it is left to
propagate as a plain OSError rather than being reported as a rejected
archive.

Cleanup only ever REMOVES what THIS call added: a target path that already
existed before this call touched it -- file or directory -- is never
queued for deletion, so a failure never makes a pre-existing path vanish.
That is narrower than "left untouched", though: extract_upload is meant to
be called with a fresh, empty destination (see its docstring) -- if it is
not, a pre-existing FILE colliding with a member name can still be
truncated or overwritten by the write attempt itself, before a later
member's failure is ever detected. Cleanup does not undo that; it only
guarantees it never deletes what collided. See _ensure_dir/_cleanup."""
from __future__ import annotations

import errno
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
MAX_NAME_LENGTH = 255                # per PATH COMPONENT, bytes (NAME_MAX on most filesystems)

# Failures that mean the archive's own data is bad -- a corrupt CRC, a
# compress_size that overruns into the next entry, or another format/feature
# problem only surfacing once a member is actually opened and read.
_ARCHIVE_FORMAT_ERRORS = (zipfile.BadZipFile, zlib.error, NotImplementedError, RuntimeError, ValueError)

# OSError errnos that mean the SERVER'S environment failed, not the upload:
# disk full, permission denied, read-only filesystem, and the like. These
# propagate as a plain OSError instead of being reported as a rejected
# archive, so the caller doesn't blame the user's upload for e.g. ENOSPC.
_ENVIRONMENT_ERRNOS = {
    errno.ENOSPC, errno.EACCES, errno.EPERM, errno.EROFS,
    errno.EIO, errno.ENOMEM, errno.EMFILE, errno.ENFILE,
}
if hasattr(errno, "EDQUOT"):
    _ENVIRONMENT_ERRNOS.add(errno.EDQUOT)


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

    ``dest`` is meant to be a fresh, empty directory -- a private
    tempfile.TemporaryDirectory(), as every caller in this codebase uses it
    -- not a directory that may already hold unrelated content. That
    precondition is what makes the collision case below unreachable in
    practice; it is documented, not defended against.

    Raises ArchiveRejected, having written nothing, for anything that is not
    a plain tree of regular files within the declared caps. Every member is
    validated in one pass before a second pass writes anything, so a
    rejection from that first pass never leaves a partial extraction behind
    -- and the second pass is itself wrapped so a failure zipfile only
    raises while actually reading a member cleans up after itself the same
    way.

    Cleanup never DELETES anything that already existed at ``dest`` before
    this call: such a path is never queued for removal. It does NOT
    guarantee such a path is left otherwise untouched -- if ``dest`` is not
    empty and a member's name collides with a pre-existing FILE,
    open(target, "wb") truncates that file the moment it is opened, before
    the member is even read, so a later failure (this call's or a
    different member's) can leave it empty or holding the archive's
    (also-rejected) content rather than what it held before."""
    dest = Path(dest)
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ArchiveRejected(f"not a zip file: {exc}") from None
    except NotImplementedError as exc:
        # E.g. a "version needed to extract" beyond what zipfile supports --
        # raised by ZipFile.__init__ itself (_RealGetContents), before any
        # of the per-member checks below ever run.
        raise ArchiveRejected(f"unsupported zip feature: {exc}") from None

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
        dest_created: list[Path] = []
        try:
            _ensure_dir(dest, dest_created)
        except OSError:
            # Narrow, environment-only case: mkdir() failed partway up a
            # chain of new parent directories (e.g. permission denied on
            # one level). Clean up what was created so far and propagate
            # unchanged -- this is a filesystem fault, not an archive one.
            _remove_empty_dirs(dest_created)
            raise
        # _ensure_dir appends in creation order (shallowest/topmost first),
        # so the topmost new directory -- the one whose removal takes
        # everything under it with it -- is the FIRST element, not the last.
        created_root = dest_created[0] if dest_created else None
        root = dest.resolve()
        touched: list[Path] = []
        created_dirs: list[Path] = []
        try:
            for info in infos:
                if info.is_dir():
                    continue
                target = (root / info.filename).resolve()
                if not target.is_relative_to(root):   # belt and braces after _check_member
                    raise ArchiveRejected(f"{info.filename}: escapes the destination")
                _ensure_dir(target.parent, created_dirs)
                # Recorded ONLY when the target did not already exist, and
                # BEFORE opening/writing it: a pre-existing path (file or
                # directory) must never be queued for removal -- deleting it
                # on failure would destroy content this call did not create
                # -- and open(target, "wb") alone creates a 0-byte file, so a
                # failure reading the member (e.g. a bad CRC-32, raised by
                # src.read() as an argument to out.write()) before a single
                # byte lands must still get that half-written file cleaned
                # up.
                if not target.exists():
                    touched.append(target)
                with archive.open(info) as src, open(target, "wb") as out:
                    out.write(src.read())
        except ArchiveRejected:
            _cleanup(created_root, touched, created_dirs)
            raise
        except OSError as exc:
            _cleanup(created_root, touched, created_dirs)
            if exc.errno in _ENVIRONMENT_ERRNOS:
                raise   # a server-side fault, not something the upload caused
            raise ArchiveRejected(f"{info.filename}: could not be extracted ({exc})") from exc
        except _ARCHIVE_FORMAT_ERRORS as exc:
            _cleanup(created_root, touched, created_dirs)
            raise ArchiveRejected(f"{info.filename}: could not be extracted ({exc})") from exc


def _ensure_dir(path: Path, created: list[Path]) -> None:
    """mkdir -p ``path``, recording each level in ``created`` AS it is
    created (not after the whole chain succeeds) -- so if mkdir() fails
    partway up the chain, ``created`` still accurately reflects what this
    call actually made, and a caller can clean up exactly that."""
    missing = []
    probe = path
    while not probe.exists():
        missing.append(probe)
        probe = probe.parent
    for d in reversed(missing):
        d.mkdir()
        created.append(d)


def _remove_empty_dirs(dirs: list[Path]) -> None:
    """Best-effort rmdir, deepest first (by path depth, not insertion
    order, so sibling subtrees created in any order are still handled
    correctly), ignoring anything not empty (holds something this call
    didn't create) or already gone."""
    for path in sorted(set(dirs), key=lambda p: len(p.parts), reverse=True):
        try:
            path.rmdir()
        except OSError:
            pass


def _cleanup(created_root: Path | None, touched: list[Path], created_dirs: list[Path]) -> None:
    """Undo exactly what this call added. If this call created ``dest`` (or
    any of dest's own parent directories, via mkdir(parents=True)),
    ``created_root`` is the topmost new directory and removing it removes
    everything under it in one shot. Otherwise, remove only the files this
    call itself wrote and the directories it created to hold them --
    deepest first, so a directory is only removed once everything this call
    put inside it is gone -- leaving anything that already existed, file or
    directory, untouched."""
    if created_root is not None:
        shutil.rmtree(created_root, ignore_errors=True)
        return
    for path in touched:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass   # best-effort: must not mask the exception already in flight
    _remove_empty_dirs(created_dirs)


def _check_member(info: zipfile.ZipInfo) -> None:
    name = info.filename
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
    # ENAMETOOLONG is a per-component limit (NAME_MAX on most filesystems),
    # not a whole-path one -- PATH_MAX covers the whole path, and depends on
    # dest's own absolute path length, which isn't something the archive
    # controls. Checking the whole name here would falsely reject a
    # legitimate, shallowly-nested export path (e.g.
    # resume/templates/<pack>/<file>) just for being long in total.
    for part in parts:
        part_len = len(part.encode("utf-8"))
        if part_len > MAX_NAME_LENGTH:
            raise ArchiveRejected(
                f"{name}: a path component is {part_len} bytes, "
                f"longer than the {MAX_NAME_LENGTH}-byte limit")
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
