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


def test_gather_already_polled_reads_config(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(
        "sources:\n"
        "  greenhouse:\n  - stripe\n"
        "  lever: []\n"
        "  workday:\n  - tenant: acme\n    region: wd5\n    site: External\n"
        "  oraclecloud:\n  - tenant: egug\n    region: us2\n    site: CX_1\n"
        "  jsonld_boards:\n"
        '  - {family: icims, slug: steeldynamics, base_url: "https://careers-steeldynamics.icims.com", company: "Steel Dynamics"}\n'
        "  eightfold:\n"
        "  - {slug: bms, domain: bms.com, flavor: pcsx, company: BMS}\n"
    )
    names = cli.gather_already_polled(p)
    assert "greenhouse:stripe" in names
    assert "workday:acme:External" in names
    assert "oraclecloud:egug:CX_1" in names
    assert "icims:steeldynamics" in names
    assert "eightfold:bms" in names


def test_json_output_written(tmp_path, monkeypatch):
    seeds_csv = tmp_path / "seeds.csv"
    seeds_csv.write_text("name,domain\n")  # empty sweep: no seeds, no network
    out = tmp_path / "results.json"
    rc = cli.main(["--seed-file", str(seeds_csv), "--out", str(out)])
    assert rc == 0
    assert json.loads(out.read_text()) == []
