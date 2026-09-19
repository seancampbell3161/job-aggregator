"""The /builder page: template pack gallery + builder settings. Fail-soft in
the provider mold (Coach/Audit): readers degrade, writers validate."""

from __future__ import annotations

import io
import logging
import re
import shutil
import zipfile
from pathlib import Path

import yaml
from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, Response
from pydantic import ValidationError

from src.tailor.render.registry import (
    SLUG_RE, TemplateInfo, builtin_slugs, get_template, list_templates, pack_info,
    user_templates_dir,
)
from src.tailor.render.settings import BuilderSettings, settings_from_dict

log = logging.getLogger(__name__)

_INT_FIELDS = ("max_bullets_per_experience", "max_bullets_per_project",
               "min_bullets_per_entry", "max_pages")
_STR_FIELDS = ("page_size", "margins")

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_UNPACKED_ZIP_BYTES = 50 * 1024 * 1024
_TEMPLATE_EXTS = (".j2", ".html")  # covers .html.j2 too (endswith .j2)

# Committed example résumé — the preview fallback when no resume_content
# document has been saved. Anchored to the repo, not the working directory.
EXAMPLE_CONTENT_PATH = Path(__file__).resolve().parents[2] / "resume" / "content.example.json"


def _require_safe_slug(slug: str) -> str:
    """Slugs are registry-generated ([a-z0-9-]); anything else (dots, slashes,
    empty) is a crafted request, not a template."""
    if not SLUG_RE.match(slug):
        raise HTTPException(status_code=400, detail="invalid template slug")
    return slug


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s or "template"


def _slug_taken(slug: str) -> bool:
    return slug in {t.slug for t in list_templates()} or (user_templates_dir() / slug).exists()


def _extract_zip_pack(data: bytes, dest: Path) -> str | None:
    """Safe zip extraction: flat pack layout, no absolute paths or traversal.
    Every member is checked before anything is written to disk. Malformed-but-
    non-traversal archives (e.g. a member `x` file and a member `x/y` nested
    under it) can still fail mid-extraction with FileExistsError/OSError; that
    is caught here and reported as a normal validation error rather than
    propagating into a 500."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return "the uploaded file is not a valid zip archive"
    for m in zf.namelist():
        p = Path(m)
        if p.is_absolute() or ".." in p.parts:
            return f"zip contains an unsafe path: {m}"
    names = set(zf.namelist())
    if "template.html.j2" not in names:
        return "zip pack must contain template.html.j2 at its root"
    unpacked_size = sum(i.file_size for i in zf.infolist())
    if unpacked_size > MAX_UNPACKED_ZIP_BYTES:
        return "unpacked size exceeds the limit"
    dest.mkdir(parents=True, exist_ok=True)
    try:
        for m in zf.namelist():
            if m.endswith("/"):
                continue
            target = dest / m
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(zf.read(m))
    except OSError as exc:
        return f"zip pack could not be extracted: {exc}"
    return None


def _write_upload_meta(dest: Path, name: str, description: str) -> None:
    """meta.yaml is generated, never hand-interpolated: a filename with a
    newline in it must not be able to inject extra YAML keys."""
    (dest / "meta.yaml").write_text(
        yaml.safe_dump({"name": name, "description": description, "source": "upload"}))


def _force_upload_meta_source(meta_path: Path) -> None:
    """A zip-supplied meta.yaml is untrusted user data: `source` is a system
    field (it decides whether the gallery shows a Delete button), so a zip
    claiming `source: builtin` must never be believed. name/description are
    kept as supplied — only source is forced. Tolerates an unreadable or
    malformed meta.yaml (treated as empty)."""
    try:
        data = yaml.safe_load(meta_path.read_text())
    except (OSError, yaml.YAMLError):
        data = None
    if not isinstance(data, dict):
        data = {}
    data["source"] = "upload"
    meta_path.write_text(yaml.safe_dump(data))


class BuilderProvider:
    def __init__(self, *, store=None, content_loader=None, importer=None, importer_source=None) -> None:
        self._store = store
        self._content_loader = content_loader
        self._importer = importer
        self._importer_source = importer_source

    @property
    def importer(self):
        """DocxTemplateImporter | None — built from current settings by
        importer_source, unless an importer was assigned explicitly."""
        if self._importer_source is not None:
            return self._importer_source()
        return self._importer

    @importer.setter
    def importer(self, value) -> None:
        self._importer = value
        self._importer_source = None  # an explicit assignment pins the importer

    def settings(self) -> BuilderSettings:
        try:
            return settings_from_dict(self._store.get() if self._store else None)
        except Exception as exc:  # noqa: BLE001 — settings must never break a page
            log.warning("builder_settings_unreadable", extra={"error": str(exc)})
            return BuilderSettings()

    def save_settings(self, form: dict[str, str]) -> str | None:
        """Parse the settings form (blank = None/default) and persist. Returns
        an error message, or None on success."""
        data: dict = {"active_template": self.settings().active_template}
        try:
            for f in _INT_FIELDS:
                v = form.get(f, "").strip()
                if v:
                    data[f] = int(v)
            for f in _STR_FIELDS:
                v = form.get(f, "").strip()
                if v:
                    data[f] = v
            validated = BuilderSettings(**data)
        except (ValueError, ValidationError) as exc:
            return str(exc)
        if self._store is None:
            return "settings storage unavailable on this backend"
        self._store.put(validated.model_dump())
        return None

    def set_active(self, slug: str) -> None:
        if self._store is None:
            return
        current = self.settings().model_dump()
        current["active_template"] = get_template(slug).slug  # normalizes unknown -> classic
        self._store.put(current)

    def content(self):
        """The saved résumé content, or the committed example as fallback —
        used for previews and upload validation renders."""
        if self._content_loader is not None:
            return self._content_loader()
        from src.tailor.content import load_content
        return load_content(EXAMPLE_CONTENT_PATH)

    def pending(self) -> list[TemplateInfo]:
        root = user_templates_dir() / ".pending"
        if not root.is_dir():
            return []
        return [pack_info(d, source="docx-import") for d in sorted(root.iterdir())
                if d.is_dir() and (d / "template.html.j2").exists()]


def _gallery(request: Request, error: str = "") -> HTMLResponse:
    prov = request.app.state.builder
    return request.app.state.templates.TemplateResponse(
        request, "_builder_gallery.html",
        {"templates": list_templates(), "pending": prov.pending(),
         "active": prov.settings().active_template, "error": error,
         "can_import": prov.importer is not None},
    )


def _stage_and_accept(request: Request, slug: str, staging: Path) -> HTMLResponse | None:
    """Run the acceptance gate on a populated staging dir and, on success,
    atomically rename it into the live template dir. Returns None on success,
    or the gallery fragment (inline error) on failure — staging is always
    cleaned up before an error is returned, so a failed upload never leaves a
    partial pack behind."""
    from src.tailor.render.validate import validate_pack
    prov = request.app.state.builder
    err = validate_pack(staging, prov.content())
    if err:
        shutil.rmtree(staging, ignore_errors=True)
        return _gallery(request, error=err)
    # Re-check right before the rename: validate_pack can take a while (it
    # renders a PDF), so the slug availability checked at the top of the
    # route may be stale by now — another upload (or a docx-import accept)
    # could have claimed it in the meantime.
    if _slug_taken(slug):
        shutil.rmtree(staging, ignore_errors=True)
        return _gallery(request, error=f"A template named '{slug}' already exists.")
    staging.rename(user_templates_dir() / slug)  # atomic accept
    return None


async def _import_docx(request: Request, slug: str, data: bytes) -> HTMLResponse:
    prov = request.app.state.builder
    if prov.importer is None:
        return _gallery(request, error="docx import is not configured (needs a configured tailoring provider).")
    if _slug_taken(slug):
        return _gallery(request, error=f"A template named '{slug}' already exists.")
    staging = user_templates_dir() / ".pending" / slug
    # Set only once staging has actually been (re)built for this request —
    # the LLM call needs no staging dir at all, so a failure there must never
    # trigger cleanup of a pre-existing pending pack of the same slug left
    # over from an earlier, already-accepted-or-still-pending import.
    rebuilt = False
    try:
        text, ex = await prov.importer.to_template(data)
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True, exist_ok=True)
        rebuilt = True
        (staging / "template.html.j2").write_text(text)
        if ex.fonts:
            (staging / "fonts").mkdir()
            for name, blob in ex.fonts:
                (staging / "fonts" / Path(name).name).write_bytes(blob)
        (staging / "meta.yaml").write_text(yaml.safe_dump(
            {"name": slug, "description": "Imported from a .docx template.", "source": "docx-import"}))
        from src.tailor.render.validate import validate_pack
        err = validate_pack(staging, prov.content())
        if err:
            shutil.rmtree(staging, ignore_errors=True)
            return _gallery(request, error=f"imported template didn't validate: {err}")
        return _gallery(request)  # stays pending until accepted
    except RuntimeError as exc:
        if rebuilt:
            shutil.rmtree(staging, ignore_errors=True)
        return _gallery(request, error=str(exc))
    except Exception as exc:  # noqa: BLE001 — a docx import must never 500
        log.warning("builder_docx_import_failed", extra={"slug": slug, "error": str(exc)})
        if rebuilt:
            shutil.rmtree(staging, ignore_errors=True)
        return _gallery(
            request, error="the import could not be processed — please try a different file.")


def register_builder_routes(app: FastAPI) -> None:
    @app.get("/builder", response_class=HTMLResponse)
    def builder(request: Request, saved: str = "", error: str = ""):
        prov = request.app.state.builder
        return request.app.state.templates.TemplateResponse(
            request, "builder.html",
            {"settings": prov.settings(), "templates": list_templates(),
             "pending": prov.pending(), "active": prov.settings().active_template,
             "saved": saved == "1", "error": error,
             "can_import": prov.importer is not None},
        )

    @app.post("/builder/settings", response_class=HTMLResponse)
    async def save_settings(request: Request):
        form = {k: str(v) for k, v in (await request.form()).items()}
        err = request.app.state.builder.save_settings(form)
        prov = request.app.state.builder
        return request.app.state.templates.TemplateResponse(
            request, "builder.html",
            {"settings": prov.settings(), "templates": list_templates(),
             "pending": prov.pending(), "active": prov.settings().active_template,
             "saved": err is None, "error": err or "",
             "can_import": prov.importer is not None},
        )

    @app.post("/builder/activate", response_class=HTMLResponse)
    def activate(request: Request, slug: str = Form(...)):
        _require_safe_slug(slug)
        request.app.state.builder.set_active(slug)
        return _gallery(request)

    @app.post("/builder/delete", response_class=HTMLResponse)
    def delete(request: Request, slug: str = Form(...)):
        _require_safe_slug(slug)
        if slug in builtin_slugs():
            raise HTTPException(status_code=400, detail="built-in templates cannot be deleted")
        target = user_templates_dir() / slug
        if not target.is_dir():
            raise HTTPException(status_code=404, detail="unknown template")
        try:
            shutil.rmtree(target)
            prov = request.app.state.builder
            if prov.settings().active_template == slug:
                prov.set_active("classic")
        except OSError as exc:
            log.warning("builder_delete_failed", extra={"slug": slug, "error": str(exc)})
            return _gallery(request, error="could not delete this template — please try again.")
        return _gallery(request)

    @app.get("/builder/preview")
    def preview(request: Request, slug: str, pending: str = ""):
        from src.tailor.models import TailorResult
        from src.tailor.render import render_resume
        _require_safe_slug(slug)
        prov = request.app.state.builder
        if pending == "1":
            pack = pack_info(user_templates_dir() / ".pending" / slug, source="docx-import")
        else:
            pack = get_template(slug)
        try:
            r = render_resume(prov.content(), TailorResult.fallback(),
                              pack=pack, settings=prov.settings())
        except Exception as exc:  # noqa: BLE001 — previews report, never 500
            log.warning("builder_preview_failed", extra={"slug": slug, "error": str(exc)})
            raise HTTPException(
                status_code=422,
                detail="preview failed — template did not render; see server logs",
            ) from exc
        return Response(content=r.pdf, media_type="application/pdf")

    @app.post("/builder/pending/accept", response_class=HTMLResponse)
    def pending_accept(request: Request, slug: str = Form(...)):
        _require_safe_slug(slug)
        staging = user_templates_dir() / ".pending" / slug
        if not staging.is_dir():
            raise HTTPException(status_code=404, detail="no such pending template")
        # Re-check right before the rename — mirrors _stage_and_accept's TOCTOU
        # guard: the slug could have been claimed by a direct upload (or
        # another accept) in the time since the pending pack was staged.
        if _slug_taken(slug):
            return _gallery(request, error=f"A template named '{slug}' already exists.")
        try:
            staging.rename(user_templates_dir() / slug)
        except OSError as exc:
            log.warning("builder_pending_accept_failed", extra={"slug": slug, "error": str(exc)})
            return _gallery(request, error="could not accept this template — please try again.")
        return _gallery(request)

    @app.post("/builder/pending/discard", response_class=HTMLResponse)
    def pending_discard(request: Request, slug: str = Form(...)):
        _require_safe_slug(slug)
        try:
            shutil.rmtree(user_templates_dir() / ".pending" / slug, ignore_errors=True)
        except OSError as exc:
            log.warning("builder_pending_discard_failed", extra={"slug": slug, "error": str(exc)})
            return _gallery(request, error="could not discard this template — please try again.")
        return _gallery(request)

    @app.post("/builder/upload", response_class=HTMLResponse)
    async def upload(request: Request, file: UploadFile):
        # Bounded read: never materialize more than we're willing to accept,
        # regardless of how large the client claims (or fails to claim) the
        # body is.
        data = await file.read(MAX_UPLOAD_BYTES + 1)
        if len(data) > MAX_UPLOAD_BYTES:
            return _gallery(request, error="File is larger than 10 MB.")

        filename = file.filename or "upload"
        stem = Path(filename).name
        for ext in (".html.j2", ".j2", ".html", ".zip", ".docx"):
            if stem.lower().endswith(ext):
                stem = stem[: -len(ext)]
                break
        slug = slugify(stem)
        lower = filename.lower()

        if lower.endswith(".docx"):
            return await _import_docx(request, slug, data)  # Task 12

        if _slug_taken(slug):
            return _gallery(request, error=f"A template named '{slug}' already exists.")

        # ".staging" is a dot-dir reserved for transient, route-local staging
        # — registry-invisible (list_templates() skips dot-prefixed dirs) and
        # distinct from ".pending", which is exclusively the docx-import
        # review namespace (BuilderProvider.pending()). A direct upload must
        # never read or clean up anything under ".pending".
        staging = user_templates_dir() / ".staging" / slug
        try:
            if staging.exists():
                shutil.rmtree(staging)

            if lower.endswith(_TEMPLATE_EXTS):
                staging.mkdir(parents=True, exist_ok=True)
                (staging / "template.html.j2").write_bytes(data)
                _write_upload_meta(staging, stem, "Uploaded template.")
            elif lower.endswith(".zip"):
                err = _extract_zip_pack(data, staging)
                if err:
                    shutil.rmtree(staging, ignore_errors=True)
                    return _gallery(request, error=err)
                meta_path = staging / "meta.yaml"
                if meta_path.exists():
                    _force_upload_meta_source(meta_path)
                else:
                    _write_upload_meta(staging, stem, "Uploaded template pack.")
            else:
                return _gallery(
                    request, error="Unsupported file type — use .html/.j2, .zip, or .docx.")

            resp = _stage_and_accept(request, slug, staging)
            return resp if resp is not None else _gallery(request)
        except Exception as exc:  # noqa: BLE001 — an upload must never 500
            log.warning("builder_upload_failed", extra={"slug": slug, "error": str(exc)})
            shutil.rmtree(staging, ignore_errors=True)
            return _gallery(
                request, error="the upload could not be processed — please try a different file.")
