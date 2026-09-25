import json

from src.config import AppConfig
from src.sqlite_db import connect
from src.starter_pack import (
    ORIGIN_EU, ORIGIN_US, StarterPack, gate_stores, load_pack, reconcile, starter_visible,
)
from src.state_sqlite import SqliteDiscoveredBoardsStore, SqliteDiscoveredSlugsStore

PACK = {
    "version": "2026-09-25",
    "slugs": [
        {"ats": "greenhouse", "slug": "stripe", "company": "Stripe", "region": "us", "postings": 300},
        {"ats": "personio", "slug": "acme-gmbh", "company": "Acme GmbH", "region": "eu", "postings": 4},
    ],
    "boards": [
        {"family": "workday", "identity": {"tenant": "3m", "region": "wd1", "site": "Search"},
         "company": "3M", "domain": "3m.com", "connector_name": "workday:3m:Search",
         "region": "us", "postings": 88},
    ],
}


def _write(tmp_path, data):
    p = tmp_path / "pack.json"
    p.write_text(json.dumps(data) if not isinstance(data, str) else data)
    return p


def _stores():
    conn = connect(":memory:")
    return SqliteDiscoveredSlugsStore(conn), SqliteDiscoveredBoardsStore(conn)


def test_load_pack_parses_entries(tmp_path):
    pack = load_pack(_write(tmp_path, PACK))
    assert pack.version == "2026-09-25"
    assert [s.connector_name for s in pack.slugs] == ["greenhouse:stripe", "personio:acme-gmbh"]
    assert pack.boards[0].connector_name == "workday:3m:Search"
    assert pack.count(eu_enabled=False) == 2
    assert pack.count(eu_enabled=True) == 3
    assert pack.postings_by_connector()["workday:3m:Search"] == 88


def test_missing_or_corrupt_pack_is_empty(tmp_path):
    assert load_pack(tmp_path / "nope.json") == StarterPack("", (), ())
    assert load_pack(_write(tmp_path, "{not json")) == StarterPack("", (), ())


def test_bad_entry_skipped_rest_kept(tmp_path):
    data = {**PACK, "slugs": PACK["slugs"] + [
        {"ats": "nosuchats", "slug": "x", "region": "us"},
        {"ats": "lever", "region": "us"},
    ], "boards": PACK["boards"] + [
        {"family": "workday", "identity": "not-a-dict", "domain": "x.com",
         "connector_name": "workday:x:y", "region": "us"},
    ]}
    pack = load_pack(_write(tmp_path, data))
    assert len(pack.slugs) == 2 and len(pack.boards) == 1


def test_reconcile_inserts_missing_us_only_when_eu_off(tmp_path):
    slugs, boards = _stores()
    pack = load_pack(_write(tmp_path, PACK))
    result = reconcile(pack, eu_enabled=False, slugs_store=slugs, boards_store=boards)
    assert (result.inserted, result.skipped) == (2, 0)
    assert slugs.get("greenhouse:stripe").origin == ORIGIN_US
    assert slugs.get("personio:acme-gmbh") is None
    assert boards.get("3m.com").origin == ORIGIN_US


def test_reconcile_eu_rows_tagged_eu(tmp_path):
    slugs, boards = _stores()
    reconcile(load_pack(_write(tmp_path, PACK)), eu_enabled=True, slugs_store=slugs, boards_store=boards)
    assert slugs.get("personio:acme-gmbh").origin == ORIGIN_EU


def test_reconcile_never_touches_existing_rows_and_is_idempotent(tmp_path):
    slugs, boards = _stores()
    slugs.upsert_failed("greenhouse:stripe", quarantine_threshold=1)
    pack = load_pack(_write(tmp_path, PACK))
    first = reconcile(pack, eu_enabled=False, slugs_store=slugs, boards_store=boards)
    assert (first.inserted, first.skipped) == (1, 1)
    assert slugs.get("greenhouse:stripe").validation_status == "quarantined"
    second = reconcile(pack, eu_enabled=False, slugs_store=slugs, boards_store=boards)
    assert (second.inserted, second.skipped) == (0, 2)


class _Spy:
    def __init__(self, inner):
        self._inner = inner
        self.calls: dict[str, int] = {}

    def __getattr__(self, name):
        attr = getattr(self._inner, name)

        def wrapped(*a, **k):
            self.calls[name] = self.calls.get(name, 0) + 1
            return attr(*a, **k)
        return wrapped


def test_reconcile_reads_keys_once_and_seeds_only_missing(tmp_path):
    slugs, boards = _stores()
    pack = load_pack(_write(tmp_path, PACK))
    reconcile(pack, eu_enabled=False, slugs_store=slugs, boards_store=boards)
    s_spy, b_spy = _Spy(slugs), _Spy(boards)
    result = reconcile(pack, eu_enabled=True, slugs_store=s_spy, boards_store=b_spy)
    assert (result.inserted, result.skipped) == (1, 2)   # only the EU slug is new
    assert s_spy.calls == {"list_all": 1, "seed_ok": 1}
    assert b_spy.calls == {"list_all": 1}


def test_starter_visible_rules():
    assert starter_visible(None, pack_enabled=False, eu_enabled=False)
    assert starter_visible("hiringcafe", pack_enabled=False, eu_enabled=False)
    assert not starter_visible(ORIGIN_US, pack_enabled=False, eu_enabled=True)
    assert starter_visible(ORIGIN_US, pack_enabled=True, eu_enabled=False)
    assert not starter_visible(ORIGIN_EU, pack_enabled=True, eu_enabled=False)
    assert starter_visible(ORIGIN_EU, pack_enabled=True, eu_enabled=True)


def test_gate_hides_starter_rows_when_off_and_restores_when_on(tmp_path):
    slugs, boards = _stores()
    slugs.upsert_ok("lever:mine")
    reconcile(load_pack(_write(tmp_path, PACK)), eu_enabled=False, slugs_store=slugs, boards_store=boards)
    off = AppConfig.model_validate({"discovery": {"starter_pack": False}})
    g_slugs, g_boards = gate_stores(off, slugs, boards)
    assert [r.connector_name for r in g_slugs.list_healthy()] == ["lever:mine"]
    assert g_boards.list_healthy() == []
    assert g_slugs.get("greenhouse:stripe") is not None  # delegation: get is unfiltered
    on = AppConfig.model_validate({"discovery": {"starter_pack": True}})
    g_slugs, g_boards = gate_stores(on, slugs, boards)
    assert {r.connector_name for r in g_slugs.list_healthy()} == {"lever:mine", "greenhouse:stripe"}
    assert len(g_boards.list_healthy()) == 1


def test_gated_revalidation_skips_hidden_rows(tmp_path):
    slugs, boards = _stores()
    reconcile(load_pack(_write(tmp_path, PACK)), eu_enabled=False, slugs_store=slugs, boards_store=boards)
    slugs.upsert_ok("lever:mine")
    off = AppConfig.model_validate({"discovery": {"starter_pack": False}})
    g_slugs, _ = gate_stores(off, slugs, boards)
    rows = g_slugs.list_for_revalidation(stale_after_days=-1, limit=10)
    assert [r.connector_name for r in rows] == ["lever:mine"]


def test_shipped_pack_file_loads_and_every_entry_builds():
    """The committed file parses, and every entry reconstructs a real connector."""
    from src.connectors.base import connector_from_identity
    from src.starter_pack import STARTER_PACK_PATH
    raw = json.loads(STARTER_PACK_PATH.read_text())
    pack = load_pack()
    assert len(pack.slugs) == len(raw["slugs"]) and len(pack.boards) == len(raw["boards"])
    for b in pack.boards:
        assert connector_from_identity(b.family, b.identity, b.company) is not None


def test_malformed_postings_skipped_rest_kept(tmp_path):
    data = {**PACK, "slugs": PACK["slugs"] + [
        {"ats": "greenhouse", "slug": "x", "company": "X", "region": "us", "postings": "lots"},
    ]}
    pack = load_pack(_write(tmp_path, data))
    assert len(pack.slugs) == 2  # only the two good entries


def test_non_list_slugs_treated_as_empty(tmp_path):
    data = {**PACK, "slugs": 5}  # not a list
    pack = load_pack(_write(tmp_path, data))
    assert len(pack.slugs) == 0
    assert len(pack.boards) == 1  # boards still loaded


def test_non_list_boards_treated_as_empty(tmp_path):
    data = {**PACK, "boards": {"not": "a list"}}
    pack = load_pack(_write(tmp_path, data))
    assert len(pack.boards) == 0
    assert len(pack.slugs) == 2  # slugs still loaded
