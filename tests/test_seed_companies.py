from scripts.seed_companies import CURATED, main
from tests.settings_helpers import seed_settings


EXPORT_TIP = ("Tip: run `python -m src.settings export DIR` before editing settings files, "
              "so this change isn't lost.")
HOST_WRITE_WARNING = ("Writing to the settings database from the host: stop the poller and web "
                      "containers first (docker compose stop poller web).")


def test_dry_run_saves_nothing(capsys):
    service = seed_settings({})
    before = len(service.versions())
    assert main(["--dry-run", "--ats", "greenhouse"]) == 0
    captured = capsys.readouterr()
    assert "[dry-run] Add greenhouse:" in captured.out
    assert EXPORT_TIP not in captured.out
    assert HOST_WRITE_WARNING not in captured.err
    assert len(service.versions()) == before


def test_adds_curated_slugs_once(capsys):
    service = seed_settings({})
    assert main(["--ats", "greenhouse"]) == 0
    captured = capsys.readouterr()
    assert HOST_WRITE_WARNING in captured.err
    assert EXPORT_TIP in captured.out
    assert service.snapshot().cfg.sources.greenhouse == list(dict.fromkeys(CURATED["greenhouse"]))
    versions = len(service.versions())
    assert main(["--ats", "greenhouse"]) == 0
    captured = capsys.readouterr()
    assert HOST_WRITE_WARNING not in captured.err  # nothing to write
    assert EXPORT_TIP not in captured.out
    assert len(service.versions()) == versions  # nothing new → no new version


def test_unknown_ats_exits_1(capsys):
    seed_settings({})
    assert main(["--ats", "workday"]) == 1
    assert "not in the curated list" in capsys.readouterr().err


def test_requires_setup(capsys):
    assert main(["--ats", "greenhouse"]) == 1
    assert "not set up" in capsys.readouterr().err
