import json
import time

import pytest

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


# ---- hardening: read-only, refuse empty, EU tagging by website ----

def test_mistyped_db_path_errors_and_creates_nothing(tmp_path, capsys):
    missing = tmp_path / "nope" / "prod.db"
    out = tmp_path / "out.json"
    assert main(["--db", str(missing), "--out", str(out)]) != 0
    assert not missing.exists() and not missing.parent.exists()
    assert not out.exists()
    assert "cannot open" in capsys.readouterr().err


def test_export_never_writes_to_the_db(tmp_path):
    path, conn = _db(tmp_path)
    conn.close()
    before = path.read_bytes()
    assert main(["--db", str(path), "--out", str(tmp_path / "o.json")]) == 0
    assert path.read_bytes() == before


def test_empty_pack_is_refused_without_allow_empty(tmp_path, capsys):
    path = tmp_path / "empty.db"
    connect(str(path)).close()   # a real, migrated, but empty DB
    out = tmp_path / "out.json"
    out.write_text("committed pack")
    assert main(["--db", str(path), "--out", str(out)]) != 0
    assert out.read_text() == "committed pack"
    assert "empty" in capsys.readouterr().err
    assert main(["--db", str(path), "--out", str(out), "--allow-empty"]) == 0
    assert json.loads(out.read_text())["slugs"] == []


def test_slug_is_tagged_eu_by_its_website_domain(tmp_path):
    conn = connect(":memory:")
    slugs = SqliteDiscoveredSlugsStore(conn)
    slugs.upsert_candidate("greenhouse:siemens", company_name="Siemens",
                           website="https://www.siemens.de/careers", claimed_family="greenhouse")
    slugs.upsert_ok("greenhouse:siemens", company_name="Siemens", last_posting_count=9)
    slugs.upsert_ok("greenhouse:stripe", company_name="Stripe", last_posting_count=9)
    pack = build_pack(conn, discovered_only=True, version="v1", eu_domains={"siemens.de"})
    regions = {s["slug"]: s["region"] for s in pack["slugs"]}
    assert regions == {"siemens": "eu", "stripe": "us"}


@pytest.mark.asyncio
async def test_chain_match_records_the_company_website():
    """The export's EU tag needs the website on the ok row the chain writes."""
    from unittest.mock import AsyncMock, patch
    from src.discovery import _run_candidate_chain
    slugs = SqliteDiscoveredSlugsStore(connect(":memory:"))
    with patch("src.discovery._probe_all_ats", new=AsyncMock(return_value=("greenhouse", 3))):
        await _run_candidate_chain(client=None, name="Siemens", website="https://siemens.de",
                                   alt_slug=None, store=slugs, boards=None,
                                   active_set=set(), budget=100)
    assert slugs.get("greenhouse:siemens").website == "https://siemens.de"
