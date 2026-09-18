"""The Backup page: download every setting as one file, or restore from one.

Registered early, alongside register_row_routes/register_companies_routes/
register_history_routes in routes.py's register_settings_routes, and for the
same reason given there: /settings/backup is a single path segment and would
otherwise be swallowed by the /settings/{slug} catch-all if that were
registered first. (/settings/backup/export and /settings/backup/import are
two segments each, so they could not collide with that catch-all either
way — they're registered alongside /settings/backup purely because this
module owns all three.)

Restoring wires together two things neither module owns on its own:
archive.extract_upload (untrusted zip bytes -> a tree of files on disk,
nothing written if the archive is rejected) and transfer.import_dir (that
tree -> one new settings version). Nothing is wrong in archive.py or
transfer.py by themselves — but this endpoint is what makes transfer.py's
LEGACY_PATH_KEYS handling reachable from an anonymous zip upload for the
first time. A pre-settings-DB config could set e.g.
`relevance.profile_path: /etc/passwd`, and import_dir would read that path
and store its contents as the profile document (which the UI then
displays) — that used to only ever run against a file the CLI operator
already had shell access to. Reachable from here, over HTTP, it would be an
authenticated arbitrary-file-read. That is why the import below passes
trust_paths=False — see import_dir's own docstring for exactly what that
does. The CLI's `settings import` command still passes the default
(trust_paths=True): there, the path is the user's own shell, and refusing to
read a file they can already read directly would protect nothing.

One exposure is still real, and worth recording rather than re-deriving:
_read_bounded below stops OUR handler from ever holding more than
MAX_UPLOAD_BYTES as one bytes object, but Starlette's own multipart parser
(FormParser/MultiPartParser in starlette/formparsers.py, checked against the
installed 1.3.1) enforces max_part_size only on non-file form fields --
on_part_data never checks it for a part that has a filename -- so by the
time our handler runs, an authenticated client can already have made the
process spool an arbitrarily large upload to a temp file on disk (not
memory: SpooledTemporaryFile rolls to disk past 1 MB, and it's unlinked at
request end via FastAPI's own UploadFile/body cleanup). This is reachable
only by a signed-in user -- the login gate runs as middleware ahead of
routing and never parses the body -- so it is a disk-exhaustion nuisance
from someone who already has an account, not an unauthenticated one. Closing
it fully would mean bypassing FastAPI's automatic multipart parsing and
reading the raw ASGI stream by hand, which is disproportionate for a
single-operator settings page."""
from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, Response
from starlette.concurrency import run_in_threadpool

from src.settings.archive import ArchiveRejected, export_zip, extract_upload
from src.settings.errors import SettingsInvalid
from src.settings.transfer import ImportFailed, ImportGuardRefused, ImportReport, import_dir
from src.web.settings.forms import errors_by_path
from src.web.settings.sections import section_by_slug
from src.web.settings.shell import page_ctx, render_section

# A generous cap on the whole upload: comfortably above what
# archive.MAX_TOTAL_BYTES (64 MB uncompressed) implies a legitimate compressed
# backup could realistically be, while still bounding what a single request
# can make this process read before extract_upload's own per-member caps
# ever run.
MAX_UPLOAD_BYTES = 32 * 1024 * 1024

# Read size for _read_bounded below — not a cap in itself, just how much
# accumulates per iteration before the running total is checked again.
_CHUNK_SIZE = 1024 * 1024


async def _read_bounded(upload: UploadFile, limit: int) -> tuple[bytes, bool]:
    """Read ``upload`` in chunks, stopping the moment the running total
    passes ``limit`` — never holding more than roughly ``limit`` bytes and
    never calling .read() with no size, which would buffer the whole body
    (however large) before anything gets a chance to check it. Returns
    (data, oversized); ``data`` is empty when oversized is True.

    Accumulates into a single bytearray (extended in place) rather than a
    list of chunks joined at the end, so a legal upload at the limit never
    transiently holds a second, equally large copy of itself in memory."""
    buf = bytearray()
    total = 0
    while True:
        chunk = await upload.read(_CHUNK_SIZE)
        if not chunk:
            return bytes(buf), False
        total += len(chunk)
        if total > limit:
            return b"", True
        buf.extend(chunk)


def _truthy(value: str) -> bool:
    return value.strip().lower() in ("on", "true", "1", "yes")


# restore_from_upload's ImportGuardRefused message is
# "{exc}\n\nNothing was written. {guard_hint}" — the head (str(exc), transfer's
# own change list, per Ruling R13) and the "Nothing was written." lead-in are
# shared verbatim by every caller; only this trailing sentence, telling the
# operator how to proceed, is caller-specific. /settings/backup/import's own
# form has the overwrite checkbox this refers to.
OVERWRITE_CHECKBOX_HINT = "Tick overwrite and upload again."


@dataclass
class RestoreOutcome:
    """What restore_from_upload produced: either ``ok`` with the
    ``ImportReport`` both callers can show, or not, with the HTTP status and
    messages each caller renders on its own page (/settings/backup/import's
    settings-shell template vs. /setup/restore's setup page)."""
    ok: bool
    status_code: int = 200
    report: ImportReport | None = None
    errors: list[str] = field(default_factory=list)


async def restore_from_upload(
    request: Request, archive: UploadFile, *, force: bool, guard_hint: str,
) -> RestoreOutcome:
    """The one implementation of "take an uploaded archive and import it":
    a bounded read (413 past MAX_UPLOAD_BYTES), extraction into a fresh
    tempfile.TemporaryDirectory(), import_dir run off the event loop, and the
    failure mapping below. Always passes trust_paths=False — see this
    module's docstring for why an uploaded archive can never be trusted the
    way the CLI's own `settings import` trusts a path on the operator's own
    shell. Both /settings/backup/import (a configured instance restoring over
    itself) and /setup/restore (an unconfigured instance's first import) call
    this rather than each running their own read/extract/import pipeline.

    ``guard_hint`` is the closing sentence of an ImportGuardRefused message
    (see OVERWRITE_CHECKBOX_HINT above) — the one part of that message that
    isn't shared verbatim, because it names how THIS caller's page lets the
    operator proceed. This matters even for /setup/restore: request.state
    .snapshot is read once, at the top of the request, before this
    (possibly slow, up to MAX_UPLOAD_BYTES) read-and-extract runs; import_dir's
    own guard is evaluated later, against whatever settings rows exist by
    then. So a real, non-default, non-import settings change landing in that
    window — another tab finishing /setup/start and then editing real
    filters or companies while this upload is still in flight — can raise
    ImportGuardRefused here even though the instance looked unconfigured when
    this request started. /setup's page has no overwrite checkbox, so its
    caller must pass a hint that doesn't claim one exists."""
    data, oversized = await _read_bounded(archive, MAX_UPLOAD_BYTES)
    if oversized:
        mb = MAX_UPLOAD_BYTES // (1024 * 1024)
        return RestoreOutcome(ok=False, status_code=413, errors=[
            f"That file is larger than the {mb} MB limit.",
        ])

    service = request.app.state.service

    def do_import() -> ImportReport:
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            extract_upload(data, dest)
            return import_dir(service, dest, force=force, trust_paths=False)

    try:
        report = await run_in_threadpool(do_import)
    except ArchiveRejected as exc:
        return RestoreOutcome(ok=False, status_code=400, errors=[str(exc)])
    except ImportGuardRefused as exc:
        # transfer._undone_changes_message ends at the "saved in:" rows
        # (Ruling R13) — the closing instruction is this surface's own,
        # not the CLI's "run export DIR / pass --force" (neither exists
        # on this page) — see guard_hint above.
        return RestoreOutcome(ok=False, status_code=409, errors=[
            f"{exc}\n\nNothing was written. {guard_hint}",
        ])
    except ImportFailed as exc:
        return RestoreOutcome(ok=False, status_code=400, errors=[str(exc)])
    except SettingsInvalid as exc:
        _, form_level = errors_by_path(exc, known=set())
        return RestoreOutcome(ok=False, status_code=400, errors=form_level)

    return RestoreOutcome(ok=True, report=report)


def _render(request: Request, *, status_code: int = 200, **extra) -> HTMLResponse:
    section = section_by_slug("backup")
    return request.app.state.templates.TemplateResponse(
        request, section.template, page_ctx(request, section, **extra),
        status_code=status_code,
    )


def register_backup_routes(app: FastAPI) -> None:
    @app.get("/settings/backup", response_class=HTMLResponse)
    def backup_page(request: Request):
        return render_section(request, section_by_slug("backup"))

    @app.get("/settings/backup/export")
    def backup_export(request: Request):
        blob = export_zip(request.app.state.service)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
        return Response(
            content=blob, media_type="application/zip",
            headers={"content-disposition":
                     f'attachment; filename="job-aggregator-settings-{stamp}.zip"'},
        )

    @app.post("/settings/backup/import", response_class=HTMLResponse)
    async def backup_import(
        request: Request, archive: UploadFile | None = None, force: str = Form(""),
    ):
        if archive is None or not archive.filename:
            return _render(request, status_code=400,
                            form_errors=["Choose a file to restore from."])

        outcome = await restore_from_upload(
            request, archive, force=_truthy(force), guard_hint=OVERWRITE_CHECKBOX_HINT,
        )
        if outcome.ok:
            return _render(request, report=outcome.report)
        return _render(request, status_code=outcome.status_code, form_errors=outcome.errors)
