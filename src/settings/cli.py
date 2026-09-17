"""python -m src.settings — manage settings stored in the app database.

Settings, documents, and secrets live in SQLite (JOB_AGG_SQLITE_PATH). Files
are an import/export format only; every change applies live in every process."""
from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path
from typing import Callable

from src.config import SLUG_SOURCE_FAMILIES
from src.settings.errors import NotConfigured, SettingsInvalid, StaleWrite
from src.settings.service import SECRET_NAMES, ConfigService, secret_env_var
from src.settings.sources import append_slug_sources
from src.settings.transfer import ImportFailed, export_dir, import_dir


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="python -m src.settings", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("import", help="load config.yaml, profile.md, resume/… from a directory")
    p.add_argument("directory", type=Path, metavar="DIR")
    p.add_argument("--config", type=Path, default=None, metavar="FILE",
                   help="config file to read instead of DIR/config.yaml")

    p = sub.add_parser("export", help="write settings and documents to a directory (never secrets)")
    p.add_argument("directory", type=Path, metavar="DIR")

    sub.add_parser("import-env-secrets",
                   help="copy non-empty JOB_AGG_* secrets from the environment into the database")

    p = sub.add_parser("set-secret", help="store a secret; the value comes from a prompt or stdin")
    p.add_argument("name", choices=SECRET_NAMES, metavar="NAME")

    p = sub.add_parser("clear-secret", help="remove a stored secret")
    p.add_argument("name", choices=SECRET_NAMES, metavar="NAME")

    p = sub.add_parser("history", help="list settings versions, newest first (* = in effect)")
    p.add_argument("--limit", type=int, default=20)

    p = sub.add_parser("restore", help="re-save an earlier settings version as the newest")
    p.add_argument("version_id", type=int, metavar="ID")

    p = sub.add_parser("add-source", help="append a company slug to a sources list")
    p.add_argument("family", choices=SLUG_SOURCE_FAMILIES, metavar="FAMILY")
    p.add_argument("slug")

    sub.add_parser("status", help="setup state, generation, degraded state, secret origins")
    return ap


def _cmd_import(service: ConfigService, args: argparse.Namespace) -> int:
    report = import_dir(service, args.directory, config_path=args.config)
    for warning in report.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    print(f"imported settings version {report.version_id} from {', '.join(report.files)}")
    if report.templates_copied:
        print(f"copied template packs: {', '.join(report.templates_copied)}")
    if report.templates_skipped:
        print(f"skipped template packs already present: {', '.join(report.templates_skipped)}")
    print("changes apply live — no restart needed")
    return 0


def _cmd_export(service: ConfigService, args: argparse.Namespace) -> int:
    written = export_dir(service, args.directory)
    print(f"exported to {args.directory}: {', '.join(written)} (secrets are never exported)")
    return 0


def _cmd_import_env_secrets(service: ConfigService, args: argparse.Namespace) -> int:
    names = service.import_env_secrets()
    if not names:
        print("no non-empty JOB_AGG_* secrets in the environment")
        return 0
    print("stored: " + ", ".join(names))
    print("env vars still take precedence while set — remove them from .env "
          "to manage these values in the database")
    return 0


def _cmd_set_secret(service: ConfigService, args: argparse.Namespace) -> int:
    if sys.stdin.isatty():
        value = getpass.getpass(f"{args.name}: ")
    else:
        value = sys.stdin.readline().rstrip("\r\n")
    service.set_secret(args.name, value)
    print(f"stored {args.name}")
    if service.secret_source(args.name) == "env":
        print(f"note: {secret_env_var(args.name)} is set in the environment and takes precedence",
              file=sys.stderr)
    return 0


def _cmd_clear_secret(service: ConfigService, args: argparse.Namespace) -> int:
    removed = service.clear_secret(args.name)
    print(f"removed {args.name}" if removed else f"{args.name} was not stored")
    return 0


def _cmd_history(service: ConfigService, args: argparse.Namespace) -> int:
    rows = service.versions(limit=args.limit)
    if not rows:
        print("no settings versions — not set up")
        return 0
    snap = service.snapshot()
    in_effect = snap.version_id if snap is not None else None
    print(f"  {'ID':>5}  {'CREATED (UTC)':<19}  {'SOURCE':<9}  NOTE")
    for row in rows:
        mark = "*" if row.id == in_effect else " "
        print(f"{mark} {row.id:>5}  {row.created_at[:19]:<19}  {row.source:<9}  {row.note or ''}")
    return 0


def _cmd_restore(service: ConfigService, args: argparse.Namespace) -> int:
    new_id = service.restore(args.version_id)
    print(f"restored version {args.version_id} as version {new_id} — applies live")
    return 0


def _cmd_add_source(service: ConfigService, args: argparse.Namespace) -> int:
    added = append_slug_sources(service, {args.family: [args.slug]}, label="add-source")
    if added:
        print(f"added {args.family}:{args.slug} — applies live")
    else:
        print(f"{args.family}:{args.slug} is already in settings")
    return 0


def _cmd_status(service: ConfigService, args: argparse.Namespace) -> int:
    snap = service.snapshot()
    if snap is None:
        print(f"setup: not configured (generation {service.generation()}) — "
              "run `python -m src.settings import DIR`")
    else:
        print(f"setup: configured (settings version {snap.version_id}, generation {snap.generation})")
        if snap.degraded is not None:
            print(f"degraded: settings version {snap.degraded.invalid_version_id} is invalid; "
                  f"running on version {snap.version_id}")
            for error in snap.degraded.errors:
                print(f"  {error['loc'] or '(document)'}: {error['msg']}")
    print("secrets:")
    for name in SECRET_NAMES:
        print(f"  {name:<26} {service.secret_source(name)}")
    return 0


_COMMANDS: dict[str, Callable[[ConfigService, argparse.Namespace], int]] = {
    "import": _cmd_import,
    "export": _cmd_export,
    "import-env-secrets": _cmd_import_env_secrets,
    "set-secret": _cmd_set_secret,
    "clear-secret": _cmd_clear_secret,
    "history": _cmd_history,
    "restore": _cmd_restore,
    "add-source": _cmd_add_source,
    "status": _cmd_status,
}


def _print_error(exc: Exception) -> None:
    if isinstance(exc, SettingsInvalid):
        print("invalid settings — nothing was written:", file=sys.stderr)
        for error in exc.errors:
            print(f"  {error['loc'] or '(document)'}: {error['msg']}", file=sys.stderr)
    else:
        print(f"error: {exc}", file=sys.stderr)


def main(argv: list[str] | None = None, *, service: ConfigService | None = None) -> int:
    args = _parser().parse_args(argv)
    if service is None:
        from src.settings import open_service
        service = open_service()
    try:
        return _COMMANDS[args.command](service, args)
    except (SettingsInvalid, NotConfigured, StaleWrite, ImportFailed, ValueError) as exc:
        _print_error(exc)
        return 1
