"""CLI tests for scripts/import_vc_portfolio.py. Library logic is covered by
tests/test_vc_portfolio.py; these exercise arg wiring + the merge path with a
monkeypatched discover_portfolio (no network)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import import_vc_portfolio as cli  # noqa: E402
from src.fingerprint import FingerprintResult  # noqa: E402


def _fake_results():
    return [FingerprintResult(name="Figma", domain="figma.com", status="matched",
                              family="greenhouse", identity={"slug": "figma"}, posting_count=7)]


def test_cli_merge_writes_matched_entry(tmp_path, monkeypatch, capsys):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("sources:\n  greenhouse:\n  - stripe\n")

    async def fake_discover(firm, *, client, config_path, limit=None, only=None, csv_path=None):
        assert firm == "a16z"
        return _fake_results()
    monkeypatch.setattr(cli, "discover_portfolio", fake_discover)
    monkeypatch.setattr(cli, "_store_names_fail_soft", lambda: set())

    rc = cli.main(["a16z", "--merge", "--config", str(cfg)])
    assert rc == 0
    loaded = yaml.safe_load(cfg.read_text())
    assert "figma" in loaded["sources"]["greenhouse"]
    assert "stripe" in loaded["sources"]["greenhouse"]  # preserved


def test_cli_dry_run_does_not_write(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    original = "sources:\n  greenhouse:\n  - stripe\n"
    cfg.write_text(original)

    async def fake_discover(firm, *, client, config_path, limit=None, only=None, csv_path=None):
        return _fake_results()
    monkeypatch.setattr(cli, "discover_portfolio", fake_discover)

    rc = cli.main(["a16z", "--config", str(cfg)])   # no --merge
    assert rc == 0
    assert cfg.read_text() == original               # untouched


def test_cli_merge_skips_already_polled(tmp_path, monkeypatch, capsys):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("sources:\n  greenhouse:\n  - figma\n")   # figma already polled

    async def fake_discover(firm, *, client, config_path, limit=None, only=None, csv_path=None):
        return _fake_results()
    monkeypatch.setattr(cli, "discover_portfolio", fake_discover)
    monkeypatch.setattr(cli, "_store_names_fail_soft", lambda: set())

    rc = cli.main(["a16z", "--merge", "--config", str(cfg)])
    assert rc == 0
    assert "Nothing new to merge" in capsys.readouterr().out
