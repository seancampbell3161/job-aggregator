"""CLI tests for scripts/import_vc_portfolio.py. Library logic is covered by
tests/test_vc_portfolio.py; these exercise arg wiring + the merge path with a
monkeypatched discover_portfolio (no network)."""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import import_vc_portfolio as cli  # noqa: E402
from src.fingerprint import FingerprintResult  # noqa: E402
from tests.settings_helpers import seed_settings  # noqa: E402


def _fake_results():
    return [FingerprintResult(name="Figma", domain="figma.com", status="matched",
                              family="greenhouse", identity={"slug": "figma"}, posting_count=7)]


EXPORT_TIP = ("Tip: run `python -m src.settings export DIR` before editing settings files, "
              "so this change isn't lost.")
HOST_WRITE_WARNING = ("Writing to the settings database from the host: stop the poller and web "
                      "containers first (docker compose stop poller web).")


def test_cli_merge_writes_matched_entry(monkeypatch, capsys):
    service = seed_settings({"sources": {"greenhouse": ["stripe"]}})

    async def fake_discover(firm, *, client, manual_companies, limit=None, only=None, csv_path=None):
        assert firm == "a16z"
        return _fake_results()
    monkeypatch.setattr(cli, "discover_portfolio", fake_discover)
    monkeypatch.setattr(cli, "_store_names_fail_soft", lambda: set())

    assert cli.main(["a16z", "--merge"]) == 0
    assert service.snapshot().cfg.sources.greenhouse == ["stripe", "figma"]
    captured = capsys.readouterr()
    assert EXPORT_TIP in captured.out
    assert HOST_WRITE_WARNING in captured.err


def test_cli_dry_run_does_not_write(monkeypatch, capsys):
    service = seed_settings({"sources": {"greenhouse": ["stripe"]}})
    before = len(service.versions())

    async def fake_discover(firm, *, client, manual_companies, limit=None, only=None, csv_path=None):
        return _fake_results()
    monkeypatch.setattr(cli, "discover_portfolio", fake_discover)

    assert cli.main(["a16z"]) == 0          # no --merge
    assert len(service.versions()) == before
    captured = capsys.readouterr()
    assert HOST_WRITE_WARNING not in captured.err
    assert EXPORT_TIP not in captured.out


def test_cli_merge_skips_already_polled(monkeypatch, capsys):
    seed_settings({"sources": {"greenhouse": ["figma"]}})

    async def fake_discover(firm, *, client, manual_companies, limit=None, only=None, csv_path=None):
        return _fake_results()
    monkeypatch.setattr(cli, "discover_portfolio", fake_discover)
    monkeypatch.setattr(cli, "_store_names_fail_soft", lambda: set())

    assert cli.main(["a16z", "--merge"]) == 0
    out = capsys.readouterr().out
    assert "Nothing new to merge" in out
    assert EXPORT_TIP not in out


def test_cli_passes_manual_companies_from_settings(monkeypatch):
    seed_settings({"discovery": {"manual_companies": ["figma"]}})
    seen = {}

    async def fake_discover(firm, *, client, manual_companies, limit=None, only=None, csv_path=None):
        seen["manual"] = set(manual_companies)
        return []
    monkeypatch.setattr(cli, "discover_portfolio", fake_discover)

    assert cli.main(["a16z"]) == 0
    assert seen["manual"] == {"figma"}


def test_cli_merge_before_setup_exits_1(monkeypatch, capsys):
    async def must_not_run(*a, **k):
        raise AssertionError("discovery must not run when --merge cannot save")
    monkeypatch.setattr(cli, "discover_portfolio", must_not_run)
    assert cli.main(["a16z", "--merge"]) == 1
    assert "not set up" in capsys.readouterr().err
