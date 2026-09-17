from scripts.seed_companies import CURATED, main
from tests.settings_helpers import seed_settings


EXPORT_TIP = ("Tip: run `python -m src.settings export DIR` before editing settings files, "
              "so this change isn't lost.")


def test_dry_run_saves_nothing(capsys):
    service = seed_settings({})
    before = len(service.versions())
    assert main(["--dry-run", "--ats", "greenhouse"]) == 0
    out = capsys.readouterr().out
    assert "[dry-run] Add greenhouse:" in out
    assert EXPORT_TIP not in out
    assert len(service.versions()) == before


def test_adds_curated_slugs_once(capsys):
    service = seed_settings({})
    assert main(["--ats", "greenhouse"]) == 0
    assert EXPORT_TIP in capsys.readouterr().out
    assert service.snapshot().cfg.sources.greenhouse == list(dict.fromkeys(CURATED["greenhouse"]))
    versions = len(service.versions())
    assert main(["--ats", "greenhouse"]) == 0
    assert EXPORT_TIP not in capsys.readouterr().out  # nothing written
    assert len(service.versions()) == versions  # nothing new → no new version


def test_unknown_ats_exits_1(capsys):
    seed_settings({})
    assert main(["--ats", "workday"]) == 1
    assert "not in the curated list" in capsys.readouterr().err


def test_requires_setup(capsys):
    assert main(["--ats", "greenhouse"]) == 1
    assert "not set up" in capsys.readouterr().err
