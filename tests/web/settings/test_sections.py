"""The partition: every editable path belongs to exactly one place."""
from src.config import Secrets
from src.settings.boards import BOARD_FAMILIES
from src.settings.fields import KIND_ROWS, editable_fields, field_map
from src.web.app import create_app
from src.web.settings.sections import (
    GROUP_TITLES, SECTIONS, UNCLAIMED_SECRETS, advanced_group, advanced_groups,
    section_by_slug, section_fields,
)
from tests.auth_helpers import signed_in_client
from tests.settings_helpers import WEB_TEST_SETTINGS, make_service


def test_no_path_is_claimed_by_two_sections():
    hand_built = [p for s in SECTIONS for p in s.paths]
    assert len(hand_built) == len(set(hand_built))


def test_advanced_groups_are_exactly_the_keys_with_leftovers():
    """Coverage itself is guaranteed by construction — _groups() builds Advanced
    as the complement of CLAIMED_PATHS, so a coverage assertion cannot fail.
    This pins the split instead: it breaks the moment a section's claims change
    which keys still have leftovers."""
    assert [g.key for g in advanced_groups()] == [
        "sources", "discovery", "gap_analysis", "tailoring", "board",
        "audit", "coach", "ops_notify", "http", "gmail",
    ]


def test_advanced_never_renders_a_claimed_path():
    claimed = {p for s in SECTIONS for p in s.paths}
    for group in advanced_groups():
        overlap = {f.path for f in group.fields} & claimed
        assert not overlap, f"{group.key} re-renders claimed paths: {sorted(overlap)}"


def test_sections_only_claim_paths_that_exist():
    known = set(field_map())
    for section in SECTIONS:
        unknown = set(section.paths) - known
        assert not unknown, f"{section.slug} claims missing paths: {sorted(unknown)}"


def test_advanced_renders_the_leftovers_of_a_partly_claimed_key():
    llm = section_by_slug("llm")
    assert "coach.provider" in llm.paths
    coach = advanced_group("coach")
    left = {f.path for f in coach.fields}
    assert "coach.provider" not in left
    assert "coach.max_jobs" in left


def test_a_fully_claimed_key_has_no_advanced_group():
    assert "filters" not in {g.key for g in advanced_groups()}


def test_companies_claims_every_source_family():
    companies = section_by_slug("companies")
    assert set(companies.paths) == {f"sources.{f}" for f in BOARD_FAMILIES}


def test_advanced_sources_keeps_only_the_aggregator_feeds():
    left = {f.path for f in advanced_group("sources").fields}
    assert not any(p == f"sources.{f}" for f in BOARD_FAMILIES for p in left)
    assert "sources.hiringcafe.enabled" in left
    assert "sources.adzuna.countries" in left


def test_the_leftover_sources_group_is_renamed_for_what_it_holds():
    assert GROUP_TITLES["sources"] == "Aggregators"
    assert advanced_group("sources").title == "Aggregators"


def test_history_and_backup_claim_nothing():
    for slug in ("history", "backup"):
        section = section_by_slug(slug)
        assert section.paths == () and section.secrets == ()


def test_section_fields_are_returned_in_claim_order():
    filters = section_by_slug("filters")
    assert [f.path for f in section_fields(filters)] == list(filters.paths)


def test_every_llm_field_belongs_to_a_group():
    llm = section_by_slug("llm")
    roots = {g.root for g in llm.groups}
    assert {p.split(".", 1)[0] for p in llm.paths} == roots


def test_llm_page_renders_group_headings_with_anchors(tmp_path, monkeypatch):
    html = signed_in_client(_app(tmp_path, monkeypatch)).get("/settings/llm").text
    for gid, title in [("scoring", "Match scoring"), ("gaps", "Skill-gap analysis"),
                        ("drafts", "Résumé drafts"), ("tailoring", "Tailored résumés"),
                        ("coach", "Coach"), ("keys", "API keys")]:
        assert f'<h3 id="{gid}">{title}</h3>' in html
    assert html.index('id="scoring"') < html.index('name="relevance.provider"') < html.index('id="gaps"')
    assert html.index('id="coach"') < html.index('name="coach.provider"') < html.index('id="keys"')


def test_nav_order_starts_at_overview_and_ends_at_backup():
    assert SECTIONS[0].slug == "overview"
    assert SECTIONS[-1].slug == "backup"


def test_secret_claims_are_real_secret_names():
    for section in SECTIONS:
        unknown = set(section.secrets) - set(Secrets.model_fields)
        assert not unknown, f"{section.slug} claims unknown secrets: {sorted(unknown)}"


def test_every_secret_is_claimed_or_explicitly_exempt():
    claimed = {name for s in SECTIONS for name in s.secrets}
    assert claimed | UNCLAIMED_SECRETS == set(Secrets.model_fields)
    assert not (claimed & UNCLAIMED_SECRETS)


def _app(tmp_path, monkeypatch, service=None):
    """Create a test app with optional service. Used for testing section saves."""
    monkeypatch.setenv("JOB_AGG_SQLITE_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("JOB_AGG_TAILORED_DIR", str(tmp_path / "tailored"))
    return create_app(service=service if service is not None else make_service(WEB_TEST_SETTINGS))


def test_every_editable_path_is_reachable_from_some_page():
    """A flag that no hand-built section claims and no Advanced group renders
    would be invisible in the UI and impossible to change without the CLI."""
    claimed = {p for s in SECTIONS for p in s.paths}
    generated = {f.path for g in advanced_groups() for f in g.fields}
    missing = {f.path for f in editable_fields()} - claimed - generated
    assert not missing, f"unreachable settings: {sorted(missing)}"


def test_no_editable_field_renders_as_read_only():
    """Every kind the UI can be asked to render has a branch in the field
    macro. read_only is the 'we cannot render this' escape hatch and should
    stay empty."""
    from src.settings.fields import KIND_READ_ONLY, editable_fields
    assert [f.path for f in editable_fields() if f.kind == KIND_READ_ONLY] == []


def test_a_bare_post_to_companies_returns_404(tmp_path, monkeypatch):
    service = make_service({"sources": {"greenhouse": ["acme"],
                                        "workday": [{"tenant": "m", "region": "wd1",
                                                     "site": "External"}]}})
    app = _app(tmp_path, monkeypatch, service)
    r = signed_in_client(app).post("/settings/companies", data={})
    assert r.status_code == 404
    cfg = app.state.service.snapshot().cfg
    assert cfg.sources.greenhouse == ["acme"]
    assert len(cfg.sources.workday) == 1
