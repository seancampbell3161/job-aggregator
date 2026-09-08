from src.sightings import Sighting, classify_sighting, drain_sightings
from src.sqlite_db import connect
from src.state_sqlite import SqliteDiscoveredBoardsStore, SqliteDiscoveredSlugsStore


def _stores():
    conn = connect(":memory:")
    return SqliteDiscoveredSlugsStore(conn), SqliteDiscoveredBoardsStore(conn)


def _s(family, slug, url, company="Acme"):
    return Sighting(ats_family=family, slug=slug, company=company, apply_url=url)


# ---------- classify_sighting ----------

def test_classified_apply_url_wins_over_claimed_label():
    s = _s("icims", "acme", "https://acme.wd5.myworkdayjobs.com/en-US/External/job/x/123")
    assert classify_sighting(s) == ("workday", {"tenant": "acme", "region": "wd5", "site": "External"})


def test_slug_family_apply_url_classifies():
    s = _s("greenhouse", "acme", "https://boards.greenhouse.io/acme/jobs/123")
    assert classify_sighting(s) == ("greenhouse", {"slug": "acme"})


def test_unclassifiable_url_falls_back_to_claimed_probeable_family():
    s = _s("lever", "acme", "https://jobs.acme.com/openings/123")
    assert classify_sighting(s) == ("lever", {"slug": "acme"})


def test_unclassifiable_url_and_unprobeable_claim_returns_none():
    assert classify_sighting(_s("icims", "acme", "https://jobs.acme.com/x")) is None
    assert classify_sighting(_s(None, None, "https://jobs.acme.com/x")) is None
    assert classify_sighting(_s("lever", None, "not a url")) is None


# ---------- drain_sightings ----------

def test_drain_routes_slug_and_board_candidates():
    slugs, boards = _stores()
    counts = drain_sightings(
        [
            _s("greenhouse", "newco", "https://jobs.acme.com/x", company="NewCo"),
            _s("workday", "acme", "https://acme.wd5.myworkdayjobs.com/en-US/Ext/job/x/1"),
        ],
        discovered=slugs, boards=boards, cap=50, no_match_fresh_days=90,
    )
    assert counts["captured_slugs"] == 1 and counts["captured_boards"] == 1
    row = slugs.get("greenhouse:newco")
    assert row.validation_status == "candidate"
    assert row.origin == "hiringcafe" and row.claimed_family == "greenhouse"
    brow = boards.get("acme.wd5.myworkdayjobs.com")
    assert brow.status == "candidate" and brow.family == "workday"
    assert brow.identity == {"tenant": "acme", "region": "wd5", "site": "Ext"}
    assert brow.connector_name == "workday:acme:Ext"


def test_drain_dedups_existing_rows_and_recent_no_match():
    slugs, boards = _stores()
    slugs.upsert_ok("greenhouse:known")
    slugs.upsert_no_match("ghost")
    boards.upsert_ok("acme.wd5.myworkdayjobs.com", name="Acme", family="workday",
                     identity={"tenant": "acme", "region": "wd5", "site": "Ext"},
                     connector_name="workday:acme:Ext")
    counts = drain_sightings(
        [
            _s("greenhouse", "known", "https://x.example/1"),
            _s("greenhouse", "ghost", "https://x.example/2"),
            _s("workday", "acme", "https://acme.wd5.myworkdayjobs.com/en-US/Ext/job/x/1"),
        ],
        discovered=slugs, boards=boards, cap=50, no_match_fresh_days=90,
    )
    assert counts["deduped"] == 3
    assert counts["captured_slugs"] == 0 and counts["captured_boards"] == 0


def test_drain_dedups_against_candidate_staging_row():
    """A VC-portfolio fetch stages `candidate:{slug}` rows ahead of any
    hiring.cafe sighting; a later sighting of the same company must dedup
    against that staging row instead of creating a duplicate `{family}:slug`
    probe candidate."""
    slugs, boards = _stores()
    slugs.upsert_candidate("candidate:acme")
    counts = drain_sightings(
        [_s("greenhouse", "acme", "https://boards.greenhouse.io/acme/jobs/123")],
        discovered=slugs, boards=boards, cap=50, no_match_fresh_days=90,
    )
    assert counts["deduped"] == 1
    assert counts["captured_slugs"] == 0
    assert slugs.get("greenhouse:acme") is None


def test_drain_respects_cap_and_counts_unclassified():
    slugs, boards = _stores()
    sightings = [_s("greenhouse", f"co{i}", "https://x.example/1") for i in range(5)]
    sightings.append(_s("icims", "nope", "https://jobs.nope.com/x"))
    counts = drain_sightings(sightings, discovered=slugs, boards=boards,
                             cap=3, no_match_fresh_days=90)
    assert counts["captured_slugs"] == 3
    assert len(slugs.list_candidates()) == 3
    assert counts["unclassified"] == 0  # cap breaks before the unclassifiable sighting
    # without the cap the unclassifiable one is counted
    counts2 = drain_sightings(sightings, discovered=slugs, boards=boards,
                              cap=50, no_match_fresh_days=90)
    assert counts2["unclassified"] == 1


def test_drain_board_path_inert_without_boards_store():
    slugs, _ = _stores()
    counts = drain_sightings(
        [_s("workday", "acme", "https://acme.wd5.myworkdayjobs.com/en-US/Ext/job/x/1")],
        discovered=slugs, boards=None, cap=50, no_match_fresh_days=90,
    )
    assert counts["no_board_store"] == 1 and counts["captured_boards"] == 0


def test_drain_eightfold_partial_identity_stages_with_none_connector_name():
    """parse_eightfold_url yields {slug, base} without domain; building the
    connector raises KeyError — the drain must stage the candidate with
    connector_name=None instead of crashing."""
    slugs, boards = _stores()
    counts = drain_sightings(
        [_s("eightfold", "acme", "https://acme.eightfold.ai/careers/job/123")],
        discovered=slugs, boards=boards, cap=50, no_match_fresh_days=90,
    )
    assert counts["captured_boards"] == 1
    row = boards.get("acme.eightfold.ai")
    assert row.status == "candidate"
    assert row.family == "eightfold"
    assert row.identity == {"slug": "acme", "base": "https://acme.eightfold.ai"}
    assert row.connector_name is None


def test_drain_is_fail_soft_per_sighting():
    slugs, boards = _stores()

    class Boom:
        def get(self, *a, **k):
            raise RuntimeError("db down")

    counts = drain_sightings(
        [
            _s("greenhouse", "boomco", "https://x.example/1"),
            _s("greenhouse", "okco", "https://x.example/2"),
        ],
        discovered=slugs, boards=boards, cap=50, no_match_fresh_days=90,
    )
    assert counts["captured_slugs"] == 2  # sanity: normal path works
    counts2 = drain_sightings(
        [_s("workday", "x", "https://x.wd1.myworkdayjobs.com/en-US/S/job/a/1")],
        discovered=slugs, boards=Boom(), cap=50, no_match_fresh_days=90,
    )
    assert counts2["captured_boards"] == 0  # swallowed, not raised


def test_eu_family_apply_url_classifies_structurally():
    assert classify_sighting(_s("other", "x", "https://sendcloud.recruitee.com/o/backend-eng")) == \
        ("recruitee", {"slug": "sendcloud"})
    assert classify_sighting(_s(None, None, "https://everphone.jobs.personio.de/job/123")) == \
        ("personio", {"slug": "everphone"})
    assert classify_sighting(_s(None, None, "https://tibber.teamtailor.com/jobs/7997072-x")) == \
        ("teamtailor", {"slug": "tibber"})


def test_eu_family_claimed_label_falls_back_to_probeable():
    # hiring.cafe labels the source + board_token but the apply URL is a branded domain
    assert classify_sighting(_s("personio", "snocks", "https://jobs.snocks.com/x")) == \
        ("personio", {"slug": "snocks"})
    assert classify_sighting(_s("recruitee", "hotjar", "https://careers.hotjar.com/x")) == \
        ("recruitee", {"slug": "hotjar"})


def test_drain_records_sighting_origin():
    from src.sightings import Sighting, drain_sightings

    class _FakeDiscovered:
        def __init__(self):
            self.upserts = []

        def get(self, key):
            return None

        def is_recent_no_match(self, slug, fresh_within_days):
            return False

        def upsert_candidate(self, key, **kw):
            self.upserts.append((key, kw))

    fake = _FakeDiscovered()
    drain_sightings(
        [Sighting(ats_family=None, slug=None, company="Acme Robotics",
                  apply_url="https://boards.greenhouse.io/acmerobotics/jobs/123",
                  origin="adzuna")],
        discovered=fake, boards=None, cap=10, no_match_fresh_days=90,
    )
    assert fake.upserts[0][0] == "greenhouse:acmerobotics"
    assert fake.upserts[0][1]["origin"] == "adzuna"


def test_sighting_origin_defaults_to_hiringcafe():
    from src.sightings import Sighting

    s = Sighting(ats_family="greenhouse", slug="acme", company="Acme", apply_url="https://x")
    assert s.origin == "hiringcafe"


def test_drain_records_sighting_origin_on_board_branch():
    from src.sightings import Sighting, drain_sightings

    class _FakeDiscovered:
        def get(self, key):
            return None

        def is_recent_no_match(self, slug, fresh_within_days):
            return False

        def upsert_candidate(self, key, **kw):
            raise AssertionError("workday sightings must route to the board store")

    class _FakeBoards:
        def __init__(self):
            self.upserts = []

        def get(self, host):
            return None

        def upsert_candidate(self, host, **kw):
            self.upserts.append((host, kw))

    boards = _FakeBoards()
    drain_sightings(
        [Sighting(ats_family=None, slug=None, company="Acme Corp",
                  apply_url="https://acme.wd1.myworkdayjobs.com/External/job/Austin/Staff-Eng_R123",
                  origin="adzuna")],
        discovered=_FakeDiscovered(), boards=boards, cap=10, no_match_fresh_days=90,
    )
    assert boards.upserts[0][0] == "acme.wd1.myworkdayjobs.com"
    assert boards.upserts[0][1]["origin"] == "adzuna"
