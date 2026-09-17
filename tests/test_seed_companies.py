from scripts.seed_companies import CURATED, main
from tests.settings_helpers import seed_settings


def test_dry_run_saves_nothing(capsys):
    service = seed_settings({})
    before = len(service.versions())
    assert main(["--dry-run", "--ats", "greenhouse"]) == 0
    assert "[dry-run] Add greenhouse:" in capsys.readouterr().out
    assert len(service.versions()) == before


def test_adds_curated_slugs_once():
    service = seed_settings({})
    assert main(["--ats", "greenhouse"]) == 0
    assert service.snapshot().cfg.sources.greenhouse == list(dict.fromkeys(CURATED["greenhouse"]))
    versions = len(service.versions())
    assert main(["--ats", "greenhouse"]) == 0
    assert len(service.versions()) == versions  # nothing new → no new version


def test_unknown_ats_exits_1(capsys):
    seed_settings({})
    assert main(["--ats", "workday"]) == 1
    assert "not in the curated list" in capsys.readouterr().err


def test_requires_setup(capsys):
    assert main(["--ats", "greenhouse"]) == 1
    assert "not set up" in capsys.readouterr().err
