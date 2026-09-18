"""Board naming: one rule, used by the UI, the dedupe, and the health join."""
from src.config import AppConfig
from src.connectors.base import build_connectors
from src.settings.boards import (
    BOARD_FAMILIES, STRUCTURED_FAMILIES, board_entries, board_key,
)
from src.fingerprint import gather_already_polled

# Covers every one of the 16 configured source families, not just the 9 in
# the module docstring's illustrative example — test_board_key_agrees_with_
# the_connectors_the_poller_builds unions build_connectors across all three
# tiers (ats/slow/headless), and workable only builds on "slow" while avature
# only builds on "headless"; a config missing either would let that family's
# board_key drift without the test ever noticing.
FULL = AppConfig.model_validate({"sources": {
    "greenhouse": ["acme"],
    "lever": ["beta"],
    "ashby": ["ashbyco"],
    "workable": ["workableco"],
    "smartrecruiters": ["smartco"],
    "rippling": ["risingco"],
    "personio": ["personioco"],
    "recruitee": ["recruiteeco"],
    "teamtailor": ["teamtailorco"],
    "workday": [{"tenant": "microsoft", "region": "wd1", "site": "External"}],
    "oraclecloud": [{"tenant": "egug", "region": "us2", "site": "CX_1", "company": "Amex"}],
    "eightfold": [{"slug": "ngc", "domain": "ngc.com"}],
    "jsonld_boards": [{"family": "icims", "slug": "kroger",
                       "base_url": "https://careers.kroger.com"}],
    "phenom": [{"careers_url": "https://careers.fisglobal.com", "company": "FIS"}],
    "taleo": [{"tenant": "cinfin", "section": "ex"}],
    "avature": [{"careers_url": "https://careers.jacobs.com/en_US/careers/SearchJobs",
                 "company": "Jacobs"}],
}})


def test_every_family_is_covered():
    assert len(BOARD_FAMILIES) == 16
    assert set(STRUCTURED_FAMILIES) <= set(BOARD_FAMILIES)
    assert "greenhouse" in BOARD_FAMILIES and "avature" in BOARD_FAMILIES


def test_keys_match_the_poller_vocabulary():
    expected = {
        "greenhouse": "greenhouse:acme",
        "workday": "workday:microsoft:External",
        "oraclecloud": "oraclecloud:egug:CX_1",
        "eightfold": "eightfold:ngc",
        "jsonld_boards": "icims:kroger",
        "taleo": "taleo:cinfin:ex",
        "phenom": "phenom:fisglobal-com",
        "avature": "avature:jacobs",
    }
    by_family = {e.family: e.key for e in board_entries(FULL)}
    for family, key in expected.items():
        assert by_family[family] == key, family


def test_board_key_agrees_with_the_connectors_the_poller_builds():
    """The whole point of board_key: it must not drift from the names
    build_connectors, the suppression set and discovered_slugs all use.
    Fails the moment a connector changes how it names itself.

    FULL configures every one of the 16 families, so this exercises all of
    them: 14 build on tier="ats", "workable" only builds on tier="slow", and
    "avature" only builds on tier="headless" (see src/connectors/base.py)."""
    entries = board_entries(FULL)
    covered_families = {e.family for e in entries}
    assert covered_families == set(BOARD_FAMILIES), (
        f"FULL doesn't configure every family: missing {set(BOARD_FAMILIES) - covered_families}"
    )
    built = {c.name for tier in ("ats", "slow", "headless")
             for c in build_connectors(FULL, tier=tier)}
    for entry in entries:
        assert entry.key in built, f"{entry.family}: {entry.key} not in {sorted(built)}"


def test_gather_already_polled_now_includes_hand_curated_families():
    names = gather_already_polled(FULL)
    assert "phenom:fisglobal-com" in names
    assert "avature:jacobs" in names
    assert "greenhouse:acme" in names


def test_label_prefers_the_company_name_then_falls_back():
    by_family = {e.family: e.label for e in board_entries(FULL)}
    assert by_family["oraclecloud"] == "Amex"
    assert by_family["phenom"] == "FIS"
    assert by_family["greenhouse"] == "acme"
    assert by_family["workday"] == "microsoft"


def test_entries_of_an_empty_config_are_empty():
    assert board_entries(AppConfig()) == []
