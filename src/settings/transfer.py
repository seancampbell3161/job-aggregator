"""Import/export between a directory of files and the settings service.

Layout (import reads it, export writes it):

    config.yaml               the settings document (export: non-default values only)
    profile.md                documents.profile
    resume.md                 documents.resume_text
    resume/content.json       documents.resume_content
    resume/evidence.json      documents.evidence
    resume/facts.yaml         documents.kit_facts
    resume/templates/<pack>/  résumé template packs (JOB_AGG_TEMPLATES_DIR)

Secrets never pass through these files in either direction."""
from __future__ import annotations

import shutil
import typing
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from pydantic import BaseModel

from src.config import AppConfig
from src.settings.errors import NotConfigured, SettingsInvalid, StaleWrite
from src.settings.import_guard import undone_changes
from src.settings.service import ConfigService
from src.settings.store import SettingsRow

DOCUMENT_FILES: dict[str, str] = {
    "profile": "profile.md",
    "resume_text": "resume.md",
    "resume_content": "resume/content.json",
    "evidence": "resume/evidence.json",
    "kit_facts": "resume/facts.yaml",
}

# Pre-settings-DB config keys that pointed at document files. On import the
# path they name (relative to the import directory) replaces the default file
# name, then the key is dropped from the stored document.
LEGACY_PATH_KEYS: dict[tuple[str, str], str] = {
    ("relevance", "profile_path"): "profile",
    ("gap_analysis", "resume_path"): "resume_text",
    ("tailoring", "content_path"): "resume_content",
    ("tailoring", "evidence_path"): "evidence",
    ("kit", "facts_path"): "kit_facts",
}

_TEMPLATE_FILENAME = "template.html.j2"

EXPORT_HEADER = (
    "# job-aggregator settings — written by `python -m src.settings export`.\n"
    "# Non-default values only; every flag and its default: docs/CONFIG.md.\n"
    "# Secrets are never exported. Apply edits with `python -m src.settings import`.\n"
)


# How many unimported settings versions an import refusal lists by name.
_MAX_LISTED_CHANGES = 10


class ImportFailed(Exception):
    """The import could not run: a file is missing, unreadable, or malformed;
    the files would undo settings saved outside an import since the last one;
    or another process saved settings mid-import. Nothing was written."""


class ImportGuardRefused(ImportFailed):
    """The specific ImportFailed raised by the undo guard (_import_base_version):
    the files would undo settings saved outside an import since the last one.
    A caller that needs to tell "please pass --force / tick overwrite" apart
    from every other import failure (missing file, bad YAML, a concurrent
    write) catches this before the broader ImportFailed."""


@dataclass
class ImportReport:
    version_id: int
    documents: dict[str, int]
    files: list[str]
    warnings: list[str] = field(default_factory=list)
    templates_copied: list[str] = field(default_factory=list)
    templates_skipped: list[str] = field(default_factory=list)


def import_dir(
    service: ConfigService, directory: Path | str, *,
    config_path: Path | str | None = None, templates_dir: Path | str | None = None,
    force: bool = False, trust_paths: bool = True,
) -> ImportReport:
    """Validate the config file and every present document, then write them
    as one settings version plus one document per file (source=import), then
    copy template packs. Nothing is written when anything fails validation.

    Import replaces the whole settings document, but add-source, the --merge
    scripts, seed_companies, and restore change settings in the database
    only — so re-importing files that predate those changes would silently
    drop them. Unless ``force``, an import refuses (ImportGuardRefused,
    nothing written) when the files would undo a setting saved outside an
    import since the last one (see import_guard.undone_changes); files that
    already carry those changes, or set a new value on purpose, import
    normally.

    ``trust_paths`` (default True, the CLI's behaviour — a user importing
    their own files on their own shell crosses no privilege boundary) governs
    LEGACY_PATH_KEYS: a pre-settings-DB config could point ``profile_path``
    (etc.) at an arbitrary file, which the CLI happily reads. A caller that
    reaches this from an untrusted upload -- e.g. the web backup-restore
    endpoint -- must pass ``trust_paths=False``: an absolute path, or a
    relative one that resolves outside ``directory``, is then ignored (a
    warning names the key; nothing about it is fatal) rather than read,
    because export_dir never writes those keys, so a file carrying one is
    either an ancient hand-made config or an attempt to read a file the
    uploader does not own by making import_dir do it on their behalf."""
    directory = Path(directory)
    config_file = Path(config_path) if config_path is not None else directory / "config.yaml"
    raw = _read_config(config_file)
    warnings: list[str] = []

    doc_paths = {kind: directory / rel for kind, rel in DOCUMENT_FILES.items()}
    for (section, key), kind in LEGACY_PATH_KEYS.items():
        block = raw.get(section)
        if not isinstance(block, dict) or key not in block:
            continue
        value = block.pop(key)
        if value:
            raw_path = Path(str(value))
            is_absolute = raw_path.is_absolute()
            path = raw_path if is_absolute else directory / raw_path
            if not trust_paths and (is_absolute or not _confined(path, directory)):
                warnings.append(
                    f"{section}.{key}: {value} points outside the archive — ignored"
                )
            else:
                doc_paths[kind] = path  # never falls back to the default-named file
                if not path.is_file():
                    warnings.append(f"{section}.{key}: {value} not found — skipped")
        if not block:
            del raw[section]
    if "secrets" in raw:
        del raw["secrets"]
        warnings.append(
            "secrets: ignored — secrets are never imported from files; "
            "use `import-env-secrets` or `set-secret`"
        )
    warnings.extend(f"{path}: unknown key, ignored" for path in unknown_keys(raw))

    documents: dict[str, str] = {}
    files = [config_file.name]
    for kind, path in doc_paths.items():
        if path.is_file():
            documents[kind] = _read_text(path)
            files.append(_display(path, directory))

    base_version_id = None if force else _import_base_version(service, raw)
    try:
        version_id, doc_ids = service.save_bundle(
            raw, documents, source="import", note="import: " + ", ".join(files),
            base_version_id=base_version_id,
        )
    except StaleWrite:
        raise ImportFailed(
            "settings were saved by another process while importing — nothing was "
            "written; run the import again"
        ) from None
    copied, skipped = _copy_packs(directory / "resume" / "templates", _templates_dir(templates_dir))
    return ImportReport(
        version_id=version_id, documents=doc_ids, files=files, warnings=warnings,
        templates_copied=copied, templates_skipped=skipped,
    )


def export_dir(
    service: ConfigService, directory: Path | str, *, templates_dir: Path | str | None = None,
) -> list[str]:
    """Write the settings version in effect, the current documents, and user
    template packs in the import layout. Returns the relative paths written."""
    current = service.current_doc()
    if current is None:
        raise NotConfigured("nothing to export — this instance is not set up")
    _, doc = current
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "config.yaml").write_text(
        EXPORT_HEADER + yaml.safe_dump(doc, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    written = ["config.yaml"]
    documents = service.documents()
    for kind, rel in DOCUMENT_FILES.items():
        body = getattr(documents, kind)
        if body is None:
            continue
        target = directory / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
        written.append(rel)
    copied, _ = _copy_packs(_templates_dir(templates_dir), directory / "resume" / "templates")
    written.extend(f"resume/templates/{name}/" for name in copied)
    return written


def unknown_keys(raw: dict, model: type[BaseModel] = AppConfig, prefix: str = "") -> list[str]:
    """Dotted paths of keys in ``raw`` that the settings model does not define
    (Pydantic would silently drop them)."""
    found: list[str] = []
    for key, value in raw.items():
        path = f"{prefix}{key}"
        model_field = model.model_fields.get(key)
        if model_field is None:
            found.append(path)
            continue
        nested = _model_in(model_field.annotation)
        if nested is None:
            continue
        if isinstance(value, dict):
            found.extend(unknown_keys(value, nested, f"{path}."))
        elif isinstance(value, list):
            for i, item in enumerate(value):
                if isinstance(item, dict):
                    found.extend(unknown_keys(item, nested, f"{path}[{i}]."))
    return found


def _model_in(annotation: object) -> type[BaseModel] | None:
    """The BaseModel inside an annotation such as Model, Model | None,
    list[Model], or list[str | Model]."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    for arg in typing.get_args(annotation):
        found = _model_in(arg)
        if found is not None:
            return found
    return None


def _import_base_version(service: ConfigService, raw: dict) -> int | None:
    """The newest settings version id (None before setup): the base version
    an import writes against, so a save landing mid-import makes it fail
    rather than be replaced. Raises ImportGuardRefused when versions saved
    some other way since the last import changed settings that ``raw`` would
    undo."""
    rows = service.versions(limit=None)  # newest first
    changes: list[SettingsRow] = []
    last_import: SettingsRow | None = None
    for row in rows:
        if row.source == "import":
            last_import = row
            break
        changes.append(row)
    if changes:
        undone = _undone_by(service, raw, last_import)
        if undone:
            raise ImportGuardRefused(_undone_changes_message(undone, changes, last_import))
    return rows[0].id if rows else None


def _confined(path: Path, directory: Path) -> bool:
    """Whether ``path`` resolves to somewhere inside ``directory`` — used by
    import_dir's trust_paths=False path to decide whether a LEGACY_PATH_KEYS
    value is safe to honour. Symlinks are resolved on both sides so a
    relative path that merely LOOKS confined but escapes via a symlink still
    counts as not confined. Anything ``resolve()`` raises over -- an OSError
    (e.g. a path component that is not a directory) or a ValueError (an
    embedded NUL byte, which PyYAML happily hands back from a quoted scalar
    like ``"a\\0b"`` and which ``Path.resolve()`` -- unlike ``.is_file()``,
    which just returns False -- raises on) -- is treated the same way: not
    confined, never left to propagate past this function."""
    try:
        return path.resolve().is_relative_to(directory.resolve())
    except (OSError, ValueError):
        return False


def _undone_by(service: ConfigService, raw: dict, last_import: SettingsRow | None) -> list[str]:
    try:
        incoming = service.parse_settings(raw)
    except SettingsInvalid:
        return []  # save_bundle reports every validation error at once; nothing is written
    current = service.current_config()
    # No usable baseline (never imported, or that version no longer validates):
    # compare strictly against the settings in effect.
    base = service.version_config(last_import.id) if last_import is not None else None
    return undone_changes(base, current[1] if current is not None else AppConfig(), incoming)


def _undone_changes_message(
    undone: list[str], changes: list[SettingsRow], last_import: SettingsRow | None,
) -> str:
    """The content every surface shares verbatim: the head, one line per
    undone setting, and the "saved in:" rows. It deliberately stops there
    (Ruling R13) rather than also picking a surface-specific instruction --
    "run `python -m src.settings export DIR` ... or pass --force" is
    meaningless on the web backup page, which has neither a DIR argument nor
    a --force flag. Each caller of ImportGuardRefused (CLI's _print_error,
    the web backup endpoint) appends its own closing instruction instead."""
    if last_import is not None:
        head = (f"these files would undo settings saved after your last import "
                f"(version {last_import.id}):")
    else:
        head = "these files would undo settings that were saved without an import:"
    lines = [head, *(f"  {line}" for line in undone), "saved in:"]
    lines += [f"  {row.id}  {row.source}  {row.note or '(no note)'}"
              for row in changes[:_MAX_LISTED_CHANGES]]
    if len(changes) > _MAX_LISTED_CHANGES:
        lines.append(f"  … and {len(changes) - _MAX_LISTED_CHANGES} more")
    return "\n".join(lines)


def _read_text(path: Path) -> str:
    """A document file's UTF-8 text; a read or decode failure is an
    ImportFailed naming the file."""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise _unreadable(path, exc) from None


def _unreadable(path: Path, exc: OSError | UnicodeDecodeError) -> ImportFailed:
    if isinstance(exc, UnicodeDecodeError):
        return ImportFailed(f"{path}: not valid UTF-8 text ({exc.reason} at byte {exc.start})")
    return ImportFailed(f"{path}: cannot read: {exc.strerror or exc}")


def _read_config(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        # path.name, not path itself: on the web backup endpoint this is a
        # tempfile.TemporaryDirectory() path that no longer exists by the
        # time anyone reads the error, and even on the CLI the full absolute
        # path adds nothing "config.yaml: not found" doesn't already say.
        raise ImportFailed(
            f"{path.name}: not found — a config file is required (start from config.example.yaml)"
        ) from None
    except IsADirectoryError:
        raise ImportFailed(f"{path} is a directory, not a file") from None
    except (OSError, UnicodeDecodeError) as exc:
        raise _unreadable(path, exc) from None
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ImportFailed(f"{path}: not valid YAML: {exc}") from exc
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ImportFailed(f"{path}: top level must be a mapping of settings sections")
    return raw


def _display(path: Path, directory: Path) -> str:
    try:
        return str(path.relative_to(directory))
    except ValueError:
        return str(path)


def _templates_dir(explicit: Path | str | None) -> Path:
    if explicit is not None:
        return Path(explicit)
    from src.tailor.render.registry import user_templates_dir
    return user_templates_dir()


def _copy_packs(src: Path, dest: Path) -> tuple[list[str], list[str]]:
    """Copy template packs (dirs holding template.html.j2) from src to dest.
    Dot-directories (.pending staging) are skipped; packs already present at
    dest are left alone."""
    if not src.is_dir():
        return [], []
    copied: list[str] = []
    skipped: list[str] = []
    for pack in sorted(src.iterdir()):
        if pack.name.startswith(".") or not (pack / _TEMPLATE_FILENAME).is_file():
            continue
        target = dest / pack.name
        if target.exists():
            skipped.append(pack.name)
            continue
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copytree(pack, target)
        copied.append(pack.name)
    return copied, skipped
