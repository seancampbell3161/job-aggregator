import json
import time

from scripts.export_starter_pack import build_pack, main
from src.sqlite_db import connect
from src.state_sqlite import (
    SqliteConnectorHealthStore, SqliteDiscoveredBoardsStore, SqliteDiscoveredSlugsStore,
)
from tests.settings_helpers import make_service

WD = {"tenant": "3m", "region": "wd1", "site": "Search"}


def _db(tmp_path):
    path = tmp_path / "prod.db"
    conn = connect(str(path))
    make_service({"sources": {"lever": ["mine"], "workday": [{"tenant": "acme", "region": "wd5", "site": "Ext"}],
                              "phenom": []}}, conn=conn)
    slugs = SqliteDiscoveredSlugsStore(conn)
    slugs.upsert_ok("greenhouse:stripe", company_name="Stripe", last_posting_count=300)
    slugs.upsert_ok("greenhouse:empty", last_posting_count=0)
    slugs.upsert_ok("ashby:dead", last_posting_count=5)
    slugs.upsert_failed("lever:bad", quarantine_threshold=1)
    slugs.upsert_ok("personio:acme-gmbh", last_posting_count=4)
    slugs.seed_ok("ashby:old", company_name="Old", origin="starter", last_posting_count=7)
    boards = SqliteDiscoveredBoardsStore(conn)
    boards.upsert_ok("3m.com", name="3M", family="workday", identity=WD,
                     connector_name="workday:3m:Search", company="3M")
    boards.upsert_ok("siemens.de", name="Siemens", family="workday",
                     identity={"tenant": "siemens", "region": "wd3", "site": "X"},
                     connector_name="workday:siemens:X", company="Siemens")
    boards.upsert_result("nope.com", name="Nope", status="not_found")
    SqliteConnectorHealthStore(conn).mark_suppressed("ashby:dead")
    conn.commit()
    return path, conn


def test_build_pack_selects_healthy_live_rows(tmp_path):
    _, conn = _db(tmp_path)
    pack = build_pack(conn, discovered_only=False, version="v1", eu_domains={"siemens.de"})
    slugs = {(s["ats"], s["slug"]): s for s in pack["slugs"]}
    assert set(slugs) == {("greenhouse", "stripe"), ("personio", "acme-gmbh"), ("lever", "mine")}
    assert slugs[("greenhouse", "stripe")] == {
        "ats": "greenhouse", "slug": "stripe", "company": "Stripe", "region": "us", "postings": 300}
    assert slugs[("personio", "acme-gmbh")]["region"] == "eu"
    boards = {b["connector_name"]: b for b in pack["boards"]}
    assert set(boards) == {"workday:3m:Search", "workday:siemens:X", "workday:acme:Ext"}
    assert boards["workday:siemens:X"]["region"] == "eu"
    assert boards["workday:acme:Ext"]["domain"] == "config:workday:acme:Ext"
    assert boards["workday:acme:Ext"]["identity"] == {"tenant": "acme", "region": "wd5", "site": "Ext"}
    assert pack["version"] == "v1"
    assert pack["slugs"] == sorted(pack["slugs"], key=lambda s: (s["ats"], s["slug"]))


def test_discovered_only_excludes_config(tmp_path):
    _, conn = _db(tmp_path)
    pack = build_pack(conn, discovered_only=True, version="v1", eu_domains=set())
    assert ("lever", "mine") not in {(s["ats"], s["slug"]) for s in pack["slugs"]}
    assert "workday:acme:Ext" not in {b["connector_name"] for b in pack["boards"]}


def test_main_writes_a_loadable_pack(tmp_path):
    from src.starter_pack import load_pack
    path, conn = _db(tmp_path)
    conn.close()
    out = tmp_path / "out.json"
    assert main(["--db", str(path), "--out", str(out), "--version", "v1"]) == 0
    pack = load_pack(out)
    assert len(pack.slugs) == 3 and len(pack.boards) == 3
    assert json.loads(out.read_text())["version"] == "v1"
