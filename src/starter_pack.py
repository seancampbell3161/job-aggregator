"""The bundled starter pack: pre-verified company boards exported from a
long-running instance (scripts/export_starter_pack.py), so a new install polls
hundreds of boards on its first check instead of none.

Pack entries become ordinary discovered rows tagged origin "starter" /
"starter:eu" — poll-health, quarantine and dead-board suppression then manage
them exactly like boards discovery found. reconcile() only ever INSERTS a
missing key; a verdict the poller already learned (quarantined, no_match, a
suppressed name) always wins over the bundled file. Whether starter rows are
polled is decided at read time by gate_stores(), so switching
discovery.starter_pack off hides them without deleting anything."""
from __future__ import annotations

import functools
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from src.fingerprint import REPO_ROOT

log = logging.getLogger(__name__)

STARTER_PACK_PATH = REPO_ROOT / "scripts" / "seeds" / "starter_pack.json"
ORIGIN_US = "starter"
ORIGIN_EU = "starter:eu"

SLUG_FAMILIES = frozenset({
    "greenhouse", "lever", "ashby", "smartrecruiters", "rippling",
    "personio", "recruitee", "teamtailor", "workable",
})
# Families connector_from_identity can rebuild (src/connectors/base.py).
BOARD_FAMILIES = frozenset(SLUG_FAMILIES - {"workable"}) | {
    "workday", "oraclecloud", "taleo", "eightfold", "jsonld",
}
_REGIONS = frozenset({"us", "eu"})


@dataclass(frozen=True)
class PackSlug:
    ats: str
    slug: str
    company: str | None
    region: str
    postings: int

    @property
    def connector_name(self) -> str:
        return f"{self.ats}:{self.slug}"


@dataclass(frozen=True)
class PackBoard:
    family: str
    identity: dict
    company: str | None
    domain: str
    connector_name: str
    region: str
    postings: int


@dataclass(frozen=True)
class StarterPack:
    version: str
    slugs: tuple[PackSlug, ...]
    boards: tuple[PackBoard, ...]

    def eligible(self, eu_enabled: bool) -> tuple[list[PackSlug], list[PackBoard]]:
        ok = lambda region: region == "us" or eu_enabled  # noqa: E731
        return ([s for s in self.slugs if ok(s.region)],
                [b for b in self.boards if ok(b.region)])

    def count(self, eu_enabled: bool) -> int:
        slugs, boards = self.eligible(eu_enabled)
        return len(slugs) + len(boards)

    def postings_by_connector(self) -> dict[str, int]:
        out = {s.connector_name: s.postings for s in self.slugs}
        out.update({b.connector_name: b.postings for b in self.boards})
        return out


_EMPTY = StarterPack("", (), ())


def _slug(e: dict) -> PackSlug | None:
    if e.get("ats") not in SLUG_FAMILIES or not e.get("slug") or e.get("region") not in _REGIONS:
        return None
    try:
        postings = int(e.get("postings") or 0)
    except (ValueError, TypeError):
        return None
    return PackSlug(ats=e["ats"], slug=str(e["slug"]), company=e.get("company"),
                    region=e["region"], postings=postings)


def _board(e: dict) -> PackBoard | None:
    if (e.get("family") not in BOARD_FAMILIES or not isinstance(e.get("identity"), dict)
            or not e.get("domain") or not e.get("connector_name")
            or e.get("region") not in _REGIONS):
        return None
    try:
        postings = int(e.get("postings") or 0)
    except (ValueError, TypeError):
        return None
    return PackBoard(family=e["family"], identity=e["identity"], company=e.get("company"),
                     domain=str(e["domain"]), connector_name=str(e["connector_name"]),
                     region=e["region"], postings=postings)


def load_pack(path: Path = STARTER_PACK_PATH) -> StarterPack:
    """Parse the pack. Never raises: a missing/corrupt file is an empty pack,
    and a malformed entry is skipped — one bad row must not cost the rest."""
    try:
        raw = json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        log.warning("starter_pack_unreadable", extra={"path": str(path), "error": str(exc)})
        return _EMPTY
    if not isinstance(raw, dict):
        log.warning("starter_pack_unreadable", extra={"path": str(path), "error": "not an object"})
        return _EMPTY
    slugs, boards, bad = [], [], 0
    slugs_list = raw.get("slugs")
    if not isinstance(slugs_list, list):
        if slugs_list is None:
            slugs_list = []
        else:
            log.warning("starter_pack_unreadable", extra={"path": str(path), "error": "slugs is not a list"})
            slugs_list = []
    for e in slugs_list:
        parsed = _slug(e) if isinstance(e, dict) else None
        if parsed:
            slugs.append(parsed)
        else:
            bad += 1
    boards_list = raw.get("boards")
    if not isinstance(boards_list, list):
        if boards_list is None:
            boards_list = []
        else:
            log.warning("starter_pack_unreadable", extra={"path": str(path), "error": "boards is not a list"})
            boards_list = []
    for e in boards_list:
        parsed = _board(e) if isinstance(e, dict) else None
        if parsed:
            boards.append(parsed)
        else:
            bad += 1
    if bad:
        log.warning("starter_pack_entries_skipped", extra={"count": bad})
    return StarterPack(str(raw.get("version") or ""), tuple(slugs), tuple(boards))


@functools.cache
def default_pack() -> StarterPack:
    """The bundled pack, read once per process (it is baked into the image)."""
    return load_pack()


def starter_pack_active(cfg) -> bool:
    """The pack is a real source: switched on AND has eligible entries. An
    empty, missing or corrupt pack polls nothing, so the flag alone must never
    count as "somewhere to look"."""
    d = cfg.discovery
    return bool(d.starter_pack) and default_pack().count(d.eu_seeds_enabled) > 0


@dataclass(frozen=True)
class ReconcileResult:
    inserted: int
    skipped: int


def _origin(region: str) -> str:
    return ORIGIN_EU if region == "eu" else ORIGIN_US


def reconcile(pack: StarterPack, *, eu_enabled: bool, slugs_store, boards_store) -> ReconcileResult:
    """Insert every eligible pack entry that has no row yet. Idempotent.

    Runs every ATS cycle, so the existing keys are read once per store and
    only missing keys are written; seed_ok stays insert-if-absent as the
    guard against a row appearing in between."""
    slugs, boards = pack.eligible(eu_enabled)
    have_slugs = {r.connector_name for r in slugs_store.list_all()}
    have_boards = {b.domain for b in boards_store.list_all()}
    inserted = skipped = 0
    for s in slugs:
        if s.connector_name not in have_slugs and slugs_store.seed_ok(
                s.connector_name, company_name=s.company,
                origin=_origin(s.region), last_posting_count=s.postings):
            inserted += 1
        else:
            skipped += 1
    for b in boards:
        if b.domain not in have_boards and boards_store.seed_ok(
                b.domain, name=b.company or b.domain, family=b.family,
                identity=b.identity, connector_name=b.connector_name,
                company=b.company, origin=_origin(b.region)):
            inserted += 1
        else:
            skipped += 1
    log.info("starter_pack_reconciled", extra={
        "version": pack.version, "inserted": inserted, "skipped": skipped,
    })
    return ReconcileResult(inserted, skipped)


def is_starter(origin: str | None) -> bool:
    """A row the starter pack seeded (and discovery has not since reclaimed)."""
    return bool(origin) and origin.startswith(ORIGIN_US)


def starter_visible(origin: str | None, *, pack_enabled: bool, eu_enabled: bool) -> bool:
    if not is_starter(origin):
        return True
    if not pack_enabled:
        return False
    return origin != ORIGIN_EU or eu_enabled


class _Gated:
    """Read-through view: delegates everything to the real store, filtering
    only the list methods the poll set and revalidation read. Writes and
    get() pass straight through, so reconcile/capture-dedup still see every
    row."""

    def __init__(self, inner, visible) -> None:
        self._inner = inner
        self._visible = visible

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def polls(self, row) -> bool:
        """Whether this row is in the poll set's view (not gated off)."""
        return self._visible(row.origin)

    def list_healthy(self):
        return [r for r in self._inner.list_healthy() if self._visible(r.origin)]


def polled(store, row) -> bool:
    """True unless ``store`` is a gated view hiding ``row``. A raw store
    (no gate) treats every row as polled."""
    polls = getattr(store, "polls", None)
    return polls is None or polls(row)


class GatedSlugs(_Gated):
    def list_for_revalidation(self, *, stale_after_days: int, limit: int):
        rows = self._inner.list_for_revalidation(stale_after_days=stale_after_days,
                                                 limit=2**31 - 1)
        return [r for r in rows if self._visible(r.origin)][:limit]


class GatedBoards(_Gated):
    pass


def gate_stores(cfg, discovered, boards) -> tuple[GatedSlugs, GatedBoards]:
    d = cfg.discovery

    def visible(origin: str | None) -> bool:
        return starter_visible(origin, pack_enabled=d.starter_pack, eu_enabled=d.eu_seeds_enabled)

    return GatedSlugs(discovered, visible), GatedBoards(boards, visible)
