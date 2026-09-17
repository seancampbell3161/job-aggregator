"""ConfigService — the one read/write path for settings, documents, and secrets.

Reads: snapshot() returns a frozen ConfigSnapshot, rebuilt only when
config_generation moves. A unit of work (poll cycle, scheduled job, web
request) takes one snapshot at its start and uses it throughout.

Writes validate first, then insert, so nothing invalid is ever stored. Stored
settings documents are canonical (secrets excluded, defaults omitted), so a
default changed in a later release reaches every install that never
overrode it."""
from __future__ import annotations

import logging
import os
import secrets
import threading
from dataclasses import dataclass
from typing import Callable, Literal, Mapping, Sequence

from pydantic import ValidationError

from src.config import AppConfig, RelevanceConfig, Secrets
from src.settings.documents import DOCUMENT_KINDS, Documents, validate_document
from src.settings.errors import NotConfigured, SettingsInvalid, StaleWrite
from src.settings.migrations import MIGRATIONS, SCHEMA_VERSION, Migration, migrate
from src.settings.store import NewDocument, NewSettings, SettingsRow, SqliteSettingsStore
from src.user_agent import set_user_agent

log = logging.getLogger(__name__)

SOURCES = frozenset({"import", "cli", "ui", "wizard", "llm_draft", "restore"})
SECRET_NAMES: tuple[str, ...] = tuple(Secrets.model_fields)
OLLAMA_HOST_ENV = "JOB_AGG_OLLAMA_HOST"
SecretSource = Literal["env", "stored", "unset"]
OllamaHostOrigin = Literal["env", "settings", "default"]


def secret_env_var(name: str) -> str:
    """The env var that overrides a stored secret: JOB_AGG_<NAME>."""
    return f"JOB_AGG_{name.upper()}"


def canonical_doc(cfg: AppConfig) -> dict:
    """The stored/exported form of a validated config: JSON-safe, secrets
    excluded, fields equal to their defaults omitted."""
    return cfg.model_dump(mode="json", exclude={"secrets"}, exclude_defaults=True)


@dataclass(frozen=True)
class Degraded:
    """The newest settings version failed migrate/validate; the snapshot runs
    on an older valid version."""

    invalid_version_id: int
    errors: list[dict]


@dataclass(frozen=True)
class ConfigSnapshot:
    generation: int
    version_id: int      # the settings version in effect
    cfg: AppConfig       # secrets resolved, env overlays applied
    documents: Documents
    degraded: Degraded | None = None


def _pydantic_errors(exc: ValidationError) -> list[dict]:
    return [{"loc": ".".join(str(p) for p in e["loc"]), "msg": e["msg"]} for e in exc.errors()]


def _check_source(source: str) -> str:
    if source not in SOURCES:
        raise ValueError(
            f"unknown settings source {source!r}; expected one of: {', '.join(sorted(SOURCES))}"
        )
    return source


def _check_secret_name(name: str) -> None:
    if name not in SECRET_NAMES:
        raise ValueError(f"unknown secret {name!r}; expected one of: {', '.join(SECRET_NAMES)}")


class ConfigService:
    def __init__(
        self,
        store: SqliteSettingsStore,
        env: Mapping[str, str] = os.environ,
        *,
        migrations: Sequence[Migration] = MIGRATIONS,
        schema_version: int = SCHEMA_VERSION,
    ) -> None:
        self._store = store
        self._env = env
        self._migrations = migrations
        self._schema_version = schema_version
        self._lock = threading.Lock()
        self._cached: tuple[int, ConfigSnapshot | None] | None = None

    # -- reads -----------------------------------------------------------------

    def generation(self) -> int:
        return self._store.generation()

    def snapshot(self) -> ConfigSnapshot | None:
        """The current settings, or None when not set up. One generation read
        when nothing changed; a rebuild only when it moved.

        The generation read, the cache check, and any rebuild all run inside
        one store.read(): otherwise a rebuild spanning several SELECTs could
        observe another thread's uncommitted write on the shared connection,
        and a phantom snapshot built from it would then sit cached under the
        current generation until the next successful write. store.read() is
        acquired before the service lock; writers never take the service
        lock, so there's no lock-ordering inversion."""
        with self._store.read():
            generation = self._store.generation()
            with self._lock:
                if self._cached is None or self._cached[0] != generation:
                    self._cached = (generation, self._build(generation))
                return self._cached[1]

    def documents(self) -> Documents:
        with self._store.read():
            bodies: dict[str, str | None] = {}
            for kind in DOCUMENT_KINDS:
                row = self._store.latest_document(kind)
                bodies[kind] = row.body if row is not None else None
            return Documents(**bodies)

    def _build(self, generation: int) -> ConfigSnapshot | None:
        effective = self._effective_version()
        if effective is None:
            return None
        row, cfg, degraded = effective
        cfg = self._with_overlays(cfg)
        set_user_agent(cfg.http.user_agent)
        return ConfigSnapshot(
            generation=generation, version_id=row.id, cfg=cfg,
            documents=self.documents(), degraded=degraded,
        )

    def _effective_version(self) -> tuple[SettingsRow, AppConfig, Degraded | None] | None:
        """The newest version that migrates and validates. When the newest one
        doesn't, walk back to the newest that does and report it degraded. When
        none does, the instance counts as not set up."""
        latest = self._store.latest_settings()
        if latest is None:
            return None
        try:
            return latest, self._parse_row(latest), None
        except SettingsInvalid as exc:
            degraded = Degraded(invalid_version_id=latest.id, errors=exc.errors)
            log.error("settings_version_invalid",
                      extra={"version_id": latest.id, "errors": exc.errors})
        for row in self._store.settings_versions(limit=None):
            if row.id >= latest.id:
                continue
            try:
                return row, self._parse_row(row), degraded
            except SettingsInvalid as exc:
                log.error("settings_version_invalid",
                          extra={"version_id": row.id, "errors": exc.errors})
        log.error("settings_no_valid_version", extra={"latest_version_id": latest.id})
        return None

    def _parse_row(self, row: SettingsRow) -> AppConfig:
        if row.doc is None:
            raise SettingsInvalid(
                [{"loc": "", "msg": f"settings version {row.id} is not valid JSON"}]
            )
        try:
            doc = migrate(row.doc, row.schema_version,
                          migrations=self._migrations, to_version=self._schema_version)
        except Exception as exc:  # noqa: BLE001 — a bad row or broken migration degrades, never crashes
            raise SettingsInvalid(
                [{"loc": "", "msg": f"cannot migrate settings version {row.id}: {exc}"}]
            ) from exc
        try:
            return self._validate(doc)
        except SettingsInvalid:
            raise
        except Exception as exc:  # noqa: BLE001 — a validator that raises something other than
            # ValidationError (e.g. a raw ZoneInfoNotFoundError) must still degrade, never crash
            # snapshot().
            raise SettingsInvalid(
                [{"loc": "", "msg": f"cannot validate settings version {row.id}: {exc}"}]
            ) from exc

    def _validate(self, doc: object) -> AppConfig:
        if not isinstance(doc, dict):
            raise SettingsInvalid([{"loc": "", "msg": "settings document must be a mapping"}])
        if "secrets" in doc:
            raise SettingsInvalid([{
                "loc": "secrets",
                "msg": "secrets are not part of the settings document — "
                       "use set-secret or import-env-secrets",
            }])
        try:
            return AppConfig.model_validate(doc)
        except ValidationError as exc:
            raise SettingsInvalid(_pydantic_errors(exc)) from exc

    def _with_overlays(self, cfg: AppConfig) -> AppConfig:
        update: dict[str, object] = {"secrets": self._resolved_secrets()}
        host = self._env.get(OLLAMA_HOST_ENV, "")
        if host:
            update["relevance"] = cfg.relevance.model_copy(update={"ollama_host": host})
        return cfg.model_copy(update=update)

    def ollama_host(self) -> tuple[str, OllamaHostOrigin]:
        """The Ollama host LLM clients use, and where it comes from: a
        non-empty JOB_AGG_OLLAMA_HOST, else the settings in effect, else (not
        set up) the model default."""
        host = self._env.get(OLLAMA_HOST_ENV, "")
        if host:
            return host, "env"
        snap = self.snapshot()
        if snap is not None:
            return snap.cfg.relevance.ollama_host, "settings"
        return RelevanceConfig().ollama_host, "default"

    # -- secrets -----------------------------------------------------------------

    def _env_secret(self, name: str) -> str:
        return self._env.get(secret_env_var(name), "")

    def _resolved_secrets(self) -> Secrets:
        stored = self._store.all_secrets()
        return Secrets(**{
            name: self._env_secret(name) or stored.get(name, "") for name in SECRET_NAMES
        })

    def secret_source(self, name: str) -> SecretSource:
        _check_secret_name(name)
        if self._env_secret(name):
            return "env"
        if self._store.get_secret(name) is not None:
            return "stored"
        return "unset"

    def set_secret(self, name: str, value: str) -> None:
        _check_secret_name(name)
        if not value:
            raise ValueError(f"{name}: value must not be empty (use clear-secret to remove a stored value)")
        self._store.put_secret(name, value)

    def clear_secret(self, name: str) -> bool:
        _check_secret_name(name)
        return self._store.delete_secret(name)

    # -- writes ------------------------------------------------------------------

    def _prepare(self, doc: object, schema_version: int | None) -> dict:
        """Migrate an incoming document (default: already current), validate
        it, and return its canonical form."""
        from_version = self._schema_version if schema_version is None else schema_version
        try:
            migrated = migrate(doc, from_version,  # type: ignore[arg-type]
                               migrations=self._migrations, to_version=self._schema_version)
        except Exception as exc:  # noqa: BLE001 — a broken migration must report as invalid, not crash the caller
            raise SettingsInvalid([{"loc": "", "msg": str(exc)}]) from exc
        return canonical_doc(self._validate(migrated))

    def save_settings(
        self, doc: dict, *, source: str, note: str | None = None,
        base_version_id: int | None = None, schema_version: int | None = None,
    ) -> int:
        """Validate, then insert as the newest version. Raises SettingsInvalid
        (nothing written) or StaleWrite when base_version_id isn't the latest."""
        _check_source(source)
        canonical = self._prepare(doc, schema_version)
        settings_id, _ = self._store.insert_bundle(
            source=source,
            settings=NewSettings(doc=canonical, note=note, schema_version=self._schema_version,
                                 base_version_id=base_version_id),
        )
        assert settings_id is not None
        return settings_id

    def save_document(
        self, kind: str, body: str, *, source: str, base_document_id: int | None = None,
    ) -> int:
        _check_source(source)
        validate_document(kind, body)
        _, ids = self._store.insert_bundle(
            source=source,
            documents=[NewDocument(kind=kind, body=body, base_document_id=base_document_id)],
        )
        return ids[0]

    def save_bundle(
        self, doc: dict, documents: Mapping[str, str], *, source: str, note: str | None = None,
        base_version_id: int | None = None,
    ) -> tuple[int, dict[str, int]]:
        """Validate a settings document and documents together, reporting every
        error at once, then write them in one transaction (one generation bump).
        Raises StaleWrite (nothing written) when base_version_id isn't the latest."""
        _check_source(source)
        errors: list[dict] = []
        canonical: dict = {}
        try:
            canonical = self._prepare(doc, None)
        except SettingsInvalid as exc:
            errors.extend(exc.errors)
        for kind, body in documents.items():
            try:
                validate_document(kind, body)
            except SettingsInvalid as exc:
                errors.extend(exc.errors)
        if errors:
            raise SettingsInvalid(errors)
        kinds = list(documents)
        settings_id, doc_ids = self._store.insert_bundle(
            source=source,
            settings=NewSettings(doc=canonical, note=note, schema_version=self._schema_version,
                                 base_version_id=base_version_id),
            documents=[NewDocument(kind=k, body=documents[k]) for k in kinds],
        )
        assert settings_id is not None
        return settings_id, dict(zip(kinds, doc_ids))

    # -- history & read-modify-write ------------------------------------------------

    def versions(self, limit: int | None = 50) -> list[SettingsRow]:
        with self._store.read():
            return self._store.settings_versions(limit)

    def current_doc(self) -> tuple[int, dict] | None:
        """(version id, canonical doc) of the settings version in effect — no
        secrets, no env overlays. The basis for export and read-modify-write."""
        with self._store.read():
            effective = self._effective_version()
        if effective is None:
            return None
        row, cfg, _ = effective
        return row.id, canonical_doc(cfg)

    def version_doc(self, version_id: int) -> dict | None:
        """Canonical document of a stored settings version; None when the
        version does not exist or no longer migrates and validates."""
        with self._store.read():
            row = self._store.get_settings_version(version_id)
            if row is None:
                return None
            try:
                return canonical_doc(self._parse_row(row))
            except SettingsInvalid:
                return None

    def canonicalize(self, doc: dict) -> dict:
        """The canonical form ``doc`` would be stored in, without writing it.
        Raises SettingsInvalid for an invalid document."""
        return self._prepare(doc, None)

    def restore(self, version_id: int) -> int:
        with self._store.read():
            row = self._store.get_settings_version(version_id)
            if row is None:
                raise SettingsInvalid([{"loc": "", "msg": f"no settings version {version_id}"}])
            doc = canonical_doc(self._parse_row(row))
        return self.save_settings(doc, source="restore", note=f"restored version {version_id}")

    def update_settings(self, mutate: Callable[[dict], str | None], *, source: str) -> int | None:
        """Read-modify-write on the version in effect.

        ``mutate`` edits the doc in place and returns the new version's note,
        or None when it changed nothing (then nothing is written). If another
        writer saves in between, the update re-reads and runs ``mutate`` once
        more — so ``mutate`` must derive everything from the doc it is given.
        Raises NotConfigured before setup; StaleWrite after a second conflict."""
        for attempt in (1, 2):
            with self._store.read():
                latest = self._store.latest_settings()
                current = self.current_doc() if latest is not None else None
            if latest is None or current is None:
                raise NotConfigured("not set up — run `python -m src.settings import DIR` first")
            current_id, doc = current
            if current_id != latest.id:
                log.warning("settings_update_replaces_invalid_version",
                            extra={"invalid_version_id": latest.id, "version_id": current_id})
            note = mutate(doc)
            if note is None:
                return None
            try:
                return self.save_settings(doc, source=source, note=note, base_version_id=latest.id)
            except StaleWrite:
                if attempt == 2:
                    raise
                log.info("settings_update_retry", extra={"source": source})
        raise AssertionError("unreachable")

    # -- secrets bootstrap -------------------------------------------------------------

    def ensure_signing_secret(self) -> None:
        """Generate the tailor deep-link signing secret once, when neither the
        env nor the DB provides one. Insert-if-absent, so a poller and a web
        process booting together converge on a single value."""
        name = "tailor_signing_secret"
        if self._env_secret(name) or self._store.get_secret(name) is not None:
            return
        if self._store.put_secret_if_absent(name, secrets.token_urlsafe(32)):
            log.info("tailor_signing_secret_generated")

    def import_env_secrets(self) -> list[str]:
        """Copy every non-empty JOB_AGG_* secret from the environment into the
        DB. Returns the names copied — never the values."""
        copied = [name for name in SECRET_NAMES if self._env_secret(name)]
        for name in copied:
            self._store.put_secret(name, self._env_secret(name))
        return copied
