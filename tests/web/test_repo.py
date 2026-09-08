import boto3
import pytest
from freezegun import freeze_time
from moto import mock_aws

from src.state import SeenJobsStore
from src.web.repo import TriageMatch, TriageRepo, filter_sort_search, workplace_type_of

TABLE = "seen_jobs_test"


def _m(job_id, *, title="Eng", company="Foo", score=5, gaps=None, status="new",
       first_seen="2026-06-10T00:00:00+00:00", comp_min=None, comp_max=None,
       posted_at=None, apply_url="https://x", history=None, location="Remote",
       workplace_type_stored=None):
    return TriageMatch(
        job_id=job_id, title=title, company=company, location_text=location,
        comp_min=comp_min, comp_max=comp_max, apply_url=apply_url, source="greenhouse:foo",
        posted_at=posted_at, score=score, rationale="r", gaps=gaps or [],
        first_seen=first_seen, status=status, history=history or [],
        workplace_type_stored=workplace_type_stored,
    )


def test_filter_by_status():
    ms = [_m("a", status="new"), _m("b", status="applied")]
    out = filter_sort_search(ms, statuses={"applied"})
    assert [m.job_id for m in out] == ["b"]


def test_filter_min_score_excludes_unscored():
    ms = [_m("a", score=8), _m("b", score=3), _m("c", score=None)]
    out = filter_sort_search(ms, min_score=5)
    assert [m.job_id for m in out] == ["a"]


def test_filter_has_gaps():
    ms = [_m("a", gaps=["Kafka"]), _m("b", gaps=[])]
    out = filter_sort_search(ms, has_gaps=True)
    assert [m.job_id for m in out] == ["a"]


def test_search_matches_title_and_company_case_insensitive():
    ms = [_m("a", title="Backend Engineer", company="Stripe"),
          _m("b", title="Designer", company="Figma")]
    assert [m.job_id for m in filter_sort_search(ms, query="stripe")] == ["a"]
    assert [m.job_id for m in filter_sort_search(ms, query="design")] == ["b"]


@pytest.mark.parametrize("location,expected", [
    ("Remote (US)", "remote"),
    ("Remote", "remote"),
    ("San Francisco, CA (Hybrid)", "hybrid"),
    ("Austin — Hybrid", "hybrid"),
    ("Remote or Hybrid", "hybrid"),          # hybrid wins over remote
    ("New York, NY", "onsite"),              # bare city, no marker → onsite
    ("Chicago (Onsite)", "onsite"),
    ("", "unknown"),
    ("   ", "unknown"),
])
def test_workplace_type_of(location, expected):
    assert workplace_type_of(location) == expected


def test_workplace_type_prefers_persisted_over_text():
    # stored "remote" wins even though the location text alone would read "onsite"
    # (this is the flag-only-remote case the text fallback gets wrong)
    assert _m("a", location="United States", workplace_type_stored="remote").workplace_type == "remote"


def test_workplace_type_falls_back_to_text_when_unpersisted():
    assert _m("a", location="Austin (Hybrid)").workplace_type == "hybrid"
    assert _m("b", location="Boston").workplace_type == "onsite"
    assert _m("c", location="").workplace_type == "unknown"


def test_filter_by_workplace_respects_persisted_value():
    # identical text, only the persisted bucket differs → filter keys off persisted
    ms = [_m("a", location="United States", workplace_type_stored="remote"),
          _m("b", location="United States", workplace_type_stored="onsite")]
    assert [m.job_id for m in filter_sort_search(ms, workplace={"remote"})] == ["a"]


def test_filter_by_workplace():
    ms = [_m("a", location="Remote (US)"), _m("b", location="Denver (Hybrid)"),
          _m("c", location="Boston"), _m("d", location="")]
    assert [m.job_id for m in filter_sort_search(ms, workplace={"remote"})] == ["a"]
    assert sorted(m.job_id for m in filter_sort_search(ms, workplace={"hybrid", "onsite"})) == ["b", "c"]
    assert [m.job_id for m in filter_sort_search(ms, workplace={"unknown"})] == ["d"]


def test_filter_by_workplace_empty_set_is_no_filter():
    ms = [_m("a", location="Remote"), _m("b", location="Boston")]
    assert sorted(m.job_id for m in filter_sort_search(ms, workplace=None)) == ["a", "b"]
    assert sorted(m.job_id for m in filter_sort_search(ms, workplace=set())) == ["a", "b"]


def test_sort_by_score_desc_then_unscored_last():
    ms = [_m("a", score=4), _m("b", score=9), _m("c", score=None)]
    assert [m.job_id for m in filter_sort_search(ms, sort="score")] == ["b", "a", "c"]


def test_sort_by_newest():
    ms = [_m("a", first_seen="2026-06-01T00:00:00+00:00"),
          _m("b", first_seen="2026-06-15T00:00:00+00:00")]
    assert [m.job_id for m in filter_sort_search(ms, sort="newest")] == ["b", "a"]


def test_comp_display():
    assert _m("a", comp_min=180000, comp_max=220000).comp_display == "$180k–$220k"
    assert _m("a", comp_min=150000, comp_max=None).comp_display == "$150k+"
    assert _m("a", comp_min=None, comp_max=None).comp_display == ""


def test_date_display_prefers_posted_at():
    assert _m("a", posted_at="2026-06-16T12:00:00+00:00",
              first_seen="2026-06-17T00:00:00+00:00").date_display == "2026-06-16"
    assert _m("a", posted_at=None, first_seen="2026-06-17T00:00:00+00:00").date_display == "2026-06-17"


def test_apply_href_guards_non_http_scheme():
    assert _m("a", apply_url="https://jobs/1").apply_href == "https://jobs/1"
    assert _m("a", apply_url="http://jobs/1").apply_href == "http://jobs/1"
    assert _m("a", apply_url="javascript:alert(1)").apply_href == "#"
    assert _m("a", apply_url="").apply_href == "#"


def test_band():
    assert _m("a", score=8).band(score_high=7, score_low=4) == "high"
    assert _m("a", score=7).band(7, 4) == "high"   # boundary: == high
    assert _m("a", score=5).band(7, 4) == "mid"
    assert _m("a", score=4).band(7, 4) == "low"    # boundary: == low
    assert _m("a", score=None).band(7, 4) == "none"


def test_current_since_uses_last_history_entry():
    m = _m("a", status="applied",
           history=[{"status": "applied", "at": "2026-06-01T10:00:00+00:00"}])
    assert m.current_since == "2026-06-01"


def test_current_since_falls_back_to_first_seen_when_no_history():
    m = _m("a", status="applied", first_seen="2026-05-20T00:00:00+00:00")
    assert m.current_since == "2026-05-20"


@freeze_time("2026-06-15T00:00:00Z")
def test_days_in_stage_counts_from_current_since():
    m = _m("a", status="applied", history=[{"status": "applied", "at": "2026-06-01T00:00:00+00:00"}])
    assert m.days_in_stage == 14


@freeze_time("2026-06-15T00:00:00Z")
def test_is_stale_only_for_active_stages_past_threshold():
    applied = _m("a", status="applied", history=[{"status": "applied", "at": "2026-06-01T00:00:00+00:00"}])
    offer = _m("b", status="offer", history=[{"status": "offer", "at": "2026-06-01T00:00:00+00:00"}])
    assert applied.is_stale(10) is True
    assert applied.is_stale(20) is False   # 14 < 20
    assert offer.is_stale(1) is False      # terminal-ish: never stale
    assert applied.staleness_label(10) == "14d · no movement"
    assert offer.staleness_label(1) == ""


def test_stage_timeline_pairs_status_and_short_date():
    m = _m("a", history=[
        {"status": "applied", "at": "2026-06-01T00:00:00+00:00"},
        {"status": "interviewing", "at": "2026-06-12T00:00:00+00:00"},
    ])
    assert m.stage_timeline == [("applied", "6/1"), ("interviewing", "6/12")]


@pytest.fixture
def repo():
    with mock_aws():
        ddb = boto3.client("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName=TABLE,
            AttributeDefinitions=[{"AttributeName": "job_id", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "job_id", "KeyType": "HASH"}],
            BillingMode="PAY_PER_REQUEST",
        )
        store = SeenJobsStore(table_name=TABLE)
        from datetime import datetime, timezone
        from src.models import NormalizedPosting
        store.claim_for_notify(
            "greenhouse:stripe:1", score=8, rationale="fit", gaps=["Kafka"],
            posting=NormalizedPosting(
                job_id="greenhouse:stripe:1", title="Senior Backend Engineer",
                company="Stripe", location_text="Remote (US)", location_tags=frozenset(),
                seniority="senior", stack=frozenset({"python"}), comp_min=180000,
                comp_max=220000, apply_url="https://x", description="",
                posted_at=datetime(2026, 6, 16, tzinfo=timezone.utc), source="greenhouse:stripe",
            ),
        )
        yield TriageRepo(store)


def test_repo_list_returns_view_models(repo):
    out = repo.list()
    assert len(out) == 1
    assert isinstance(out[0], TriageMatch)
    assert out[0].company == "Stripe"


def test_repo_get_and_set_status(repo):
    assert repo.get("greenhouse:stripe:1").status == "new"
    assert repo.set_status("greenhouse:stripe:1", "applied") is True
    assert repo.get("greenhouse:stripe:1").status == "applied"
    assert repo.get("nope:0:0") is None


@pytest.fixture
def sqlite_repo():
    """A repo over a real SQLite store — add_manual writes, so it needs a store
    that round-trips rather than the read-only stubs used above."""
    from src.sqlite_db import connect
    from src.state_sqlite import SqliteSeenJobsStore
    conn = connect(":memory:")
    return TriageRepo(SqliteSeenJobsStore(conn)), conn


@freeze_time("2026-08-14T18:42:30Z")
def test_add_manual_writes_a_notified_match_at_the_chosen_stage(sqlite_repo):
    repo, _ = sqlite_repo
    job_id = repo.add_manual(
        company="Northwind Labs", title="Staff Platform Engineer", status="applied",
        location_text="Remote (US)", apply_url="https://northwind.example/jobs/42",
        comp_min=210000, comp_max=250000, channel="LinkedIn recruiter",
        description="Own the platform team.",
    )
    assert job_id == "manual:northwind-labs-staff-platform-engineer:20260814184230"
    m = repo.get(job_id)
    assert (m.company, m.title, m.status) == ("Northwind Labs", "Staff Platform Engineer", "applied")
    assert m.source == "manual:linkedin-recruiter" and m.is_manual is True
    assert m.comp_display == "$210k–$250k"
    assert m.apply_href == "https://northwind.example/jobs/42"
    assert m.score is None                       # nothing scored it
    assert [h["status"] for h in m.history] == ["applied"]
    assert repo.list()[0].job_id == job_id       # shows up in triage/board reads


def test_add_manual_row_has_no_ttl_so_it_never_expires(sqlite_repo):
    repo, conn = sqlite_repo
    job_id = repo.add_manual(company="Acme", title="Eng")
    assert conn.execute("SELECT ttl FROM seen_jobs WHERE job_id = ?", (job_id,)).fetchone()[0] is None


def test_add_manual_keeps_the_description_for_tailoring(sqlite_repo):
    repo, _ = sqlite_repo
    job_id = repo.add_manual(company="Acme", title="Eng", description="Ship Python services.")
    assert repo._store.get_jd(job_id).description == "Ship Python services."


def test_add_manual_rejects_blank_company_or_title_and_bad_status(sqlite_repo):
    repo, _ = sqlite_repo
    for kwargs in ({"company": "  ", "title": "Eng"}, {"company": "Acme", "title": ""}):
        with pytest.raises(ValueError, match="required"):
            repo.add_manual(**kwargs)
    with pytest.raises(ValueError, match="invalid status"):
        repo.add_manual(company="Acme", title="Eng", status="banana")


@freeze_time("2026-08-14T18:42:30Z")
def test_add_manual_same_second_duplicate_gets_its_own_id(sqlite_repo):
    repo, _ = sqlite_repo
    first = repo.add_manual(company="Acme", title="Eng")
    second = repo.add_manual(company="Acme", title="Eng")
    assert first != second and second.endswith("-1")
    assert len(repo.list()) == 2


@freeze_time("2026-08-14T18:42:30Z")
def test_add_manual_keeps_typed_names_out_of_the_store_key(sqlite_repo):
    # ':' separates source from external id and is not stripped by the shared
    # slug rule, so the id builder narrows to [a-z0-9-] itself.
    repo, _ = sqlite_repo
    job_id = repo.add_manual(company="Ünïcode: Foo/Bar™", title="Eng", channel="a:b™")
    assert job_id == "manual:ncode-foobar-eng:20260814184230"
    assert repo.get(job_id).source == "manual:ab"


def test_is_manual_only_for_the_manual_source_family():
    assert _m("a").is_manual is False                                  # greenhouse:foo
    manual = TriageMatch(
        job_id="manual:x:1", title="T", company="C", location_text="", comp_min=None,
        comp_max=None, apply_url="", source="manual:recruiter", posted_at=None,
        score=None, rationale=None, gaps=[], first_seen="", status="interested", history=[],
    )
    assert manual.is_manual is True


def test_closed_label_from_posting_closed_at():
    from datetime import datetime, timedelta, timezone
    from src.web.repo import TriageMatch
    three_days = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    m = TriageMatch(
        job_id="x:1", title="T", company="C", location_text="", comp_min=None,
        comp_max=None, apply_url="", source="", posted_at=None, score=None,
        rationale=None, gaps=[], first_seen="", status="applied", history=[],
        posting_closed_at=three_days,
    )
    assert m.closed_label() == "posting closed 3d ago"
    m2 = TriageMatch(
        job_id="x:2", title="T", company="C", location_text="", comp_min=None,
        comp_max=None, apply_url="", source="", posted_at=None, score=None,
        rationale=None, gaps=[], first_seen="", status="applied", history=[],
    )
    assert m2.closed_label() == ""
