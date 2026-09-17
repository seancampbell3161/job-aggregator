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
from src.settings.errors import NotConfigured
from src.settings.service import ConfigService

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


class ImportFailed(Exception):
    """The import directory's config file is missing, unreadable, or malformed."""


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
) -> ImportReport:
    """Validate the config file and every present document, then write them
    as one settings version plus one document per file (source=import), then
    copy template packs. Nothing is written when anything fails validation."""
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
            path = Path(str(value))
            doc_paths[kind] = path if path.is_absolute() else directory / path
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
            documents[kind] = path.read_text(encoding="utf-8")
            files.append(_display(path, directory))

    version_id, doc_ids = service.save_bundle(
        raw, documents, source="import", note="import: " + ", ".join(files),
    )
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


def _read_config(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ImportFailed(
            f"{path}: not found — a config file is required (start from config.example.yaml)"
        ) from None
    except IsADirectoryError:
        raise ImportFailed(f"{path} is a directory, not a file") from None
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
