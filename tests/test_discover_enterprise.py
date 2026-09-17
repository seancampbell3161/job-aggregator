# tests/test_discover_enterprise.py
import json
from pathlib import Path

from src.fingerprint import FingerprintResult

# Same import mechanism as tests/test_import_vc_portfolio.py:
import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import discover_enterprise as cli  # noqa: E402


def _r(status, name="Acme", **kw):
    return FingerprintResult(name=name, domain=f"{name.lower()}.com", status=status, **kw)


def test_format_report_groups_by_status():
    results = [
        _r("matched", "Acme", family="workday",
           identity={"tenant": "acme", "region": "wd5", "site": "External"}, posting_count=42),
        _r("unsupported", "Walled", family="icims", evidence_url="https://careers-walled.icims.com/x"),
        _r("not_found", "Plain"),
        _r("error", "Broken", note="ConnectError: down"),
    ]
    report = cli.format_report(results)
    assert "MATCHED (1)" in report and "UNSUPPORTED (1)" in report
    assert "NOT_FOUND (1)" in report and "ERROR (1)" in report
    assert "workday:acme:External" in report and "42" in report
    assert "icims" in report and "careers-walled.icims.com" in report


def test_gather_already_polled_reads_a_settings_config():
    from src.config import AppConfig
    from src.fingerprint import gather_already_polled
    cfg = AppConfig.model_validate({"sources": {
        "greenhouse": ["stripe"],
        "workday": [{"tenant": "acme", "region": "wd5", "site": "External"}],
        "oraclecloud": [{"tenant": "egug", "region": "us2", "site": "CX_1"}],
        "jsonld_boards": [{"family": "icims", "slug": "steeldynamics",
                           "base_url": "https://careers-steeldynamics.icims.com",
                           "company": "Steel Dynamics"}],
        "eightfold": [{"slug": "bms", "domain": "bms.com", "flavor": "pcsx", "company": "BMS"}],
    }})
    names = gather_already_polled(cfg)
    assert {"greenhouse:stripe", "workday:acme:External", "oraclecloud:egug:CX_1",
            "icims:steeldynamics", "eightfold:bms"} <= names


def test_json_output_written(tmp_path, monkeypatch):
    seeds_csv = tmp_path / "seeds.csv"
    seeds_csv.write_text("name,domain\n")  # empty sweep: no seeds, no network
    out = tmp_path / "results.json"
    rc = cli.main(["--seed-file", str(seeds_csv), "--out", str(out)])
    assert rc == 0
    assert json.loads(out.read_text()) == []


def _one_seed_csv(tmp_path):
    seeds_csv = tmp_path / "seeds.csv"
    seeds_csv.write_text("name,domain\nAcme,acme.com\n")
    return seeds_csv


async def _fake_fingerprint_many(seeds):
    return [_r("matched", "Acme", family="workday",
               identity={"tenant": "acme", "region": "wd5", "site": "External"}, posting_count=42)]


def test_merge_saves_matched_entries_into_settings(tmp_path, monkeypatch, capsys):
    from tests.settings_helpers import seed_settings
    service = seed_settings({"sources": {"greenhouse": ["stripe"]}})
    monkeypatch.setattr(cli, "fingerprint_many", _fake_fingerprint_many)
    monkeypatch.setattr(cli, "_store_names_fail_soft", lambda: set())
    rc = cli.main(["--seed-file", str(_one_seed_csv(tmp_path)),
                   "--out", str(tmp_path / "r.json"), "--merge"])
    assert rc == 0
    assert "Merged 1 new entries into settings" in capsys.readouterr().out
    assert [w.tenant for w in service.snapshot().cfg.sources.workday] == ["acme"]


def test_merge_before_setup_exits_1_without_sweeping(tmp_path, monkeypatch, capsys):
    async def must_not_run(seeds):
        raise AssertionError("the sweep must not start when --merge cannot save")
    monkeypatch.setattr(cli, "fingerprint_many", must_not_run)
    rc = cli.main(["--seed-file", str(_one_seed_csv(tmp_path)), "--merge"])
    assert rc == 1
    assert "not set up" in capsys.readouterr().err
