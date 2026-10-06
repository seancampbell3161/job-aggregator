from scripts.pack_builder.cache import CheckResult
from scripts.pack_builder.select import (
    EU_LIMITS, US_LIMITS, Limits, LiveBoard, fill, region_of, select, to_pack,
)
from src.starter_pack import load_pack, write_pack


def _b(key, postings, us=0, eu=0, family="ashby"):
    ident = ({"tenant": key, "region": "wd1", "site": "S"} if family == "workday"
             else {"slug": key})
    name = f"workday:{key}:S" if family == "workday" else f"{family}:{key}"
    return LiveBoard(key, family, CheckResult("live", postings, us, eu, key.title(), name, ident))


def test_region_of():
    assert region_of(CheckResult("live", 5, 1, 4)) == "us"
    assert region_of(CheckResult("live", 5, 0, 4)) == "eu"
    assert region_of(CheckResult("live", 5, 0, 0)) is None


def test_fill_ranks_by_regional_count_and_skips_what_does_not_fit():
    boards = [_b("huge", 90, us=90), _b("big", 60, us=60), _b("mid", 30, us=30), _b("small", 5, us=5)]
    pick = fill(boards, Limits(max_boards=10, max_postings=100), "us")
    # huge (90) fits; big (60) would bust 100 → skipped; mid (30) would bust → skipped; small fits.
    assert [b.key for b in pick.boards] == ["huge", "small"]
    assert pick.postings == 95 and pick.binding == "postings"


def test_fill_stops_at_board_limit():
    pick = fill([_b(f"b{i}", 1, us=1) for i in range(5)], Limits(3, 1_000), "us")
    assert len(pick.boards) == 3 and pick.binding == "boards"


def test_fill_ties_break_on_total_postings_then_key():
    boards = [_b("b", 10, us=5), _b("a", 10, us=5), _b("c", 20, us=5)]
    assert [b.key for b in fill(boards, Limits(10, 1_000), "us").boards] == ["c", "a", "b"]


def test_select_splits_regions_and_drops_neither():
    picks = select([_b("u", 5, us=5), _b("e", 5, eu=5), _b("x", 5)])
    assert [b.key for b in picks["us"].boards] == ["u"]
    assert [b.key for b in picks["eu"].boards] == ["e"]


def test_combined_limits_fit_what_polling_handles_today():
    assert US_LIMITS.max_boards + EU_LIMITS.max_boards <= 5_000
    assert US_LIMITS.max_postings + EU_LIMITS.max_postings <= 120_000


def test_to_pack_round_trips_through_the_loader(tmp_path):
    picks = select([_b("acme", 12, us=3), _b("bigco", 40, us=40, family="workday"),
                    _b("gmbh", 4, eu=4)])
    pack = to_pack(picks, "2026-10-06")
    out = tmp_path / "pack.json"
    write_pack(pack, out)
    loaded = load_pack(out)
    assert {s.connector_name for s in loaded.slugs} == {"ashby:acme", "ashby:gmbh"}
    [board] = loaded.boards
    assert board.connector_name == "workday:bigco:S" and board.domain == "pack:workday:bigco:S"
    assert {s.region for s in loaded.slugs} == {"us", "eu"}
    assert loaded.postings_by_connector()["workday:bigco:S"] == 40
