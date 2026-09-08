# tests/test_fingerprint.py
from pathlib import Path

import pytest

from src.fingerprint import Seed, load_seeds, parse_ats_url

SEED_CSV = Path("scripts/seeds/enterprise_companies.csv")


def test_load_seeds_roundtrip(tmp_path):
    p = tmp_path / "seeds.csv"
    p.write_text("name,domain\nAmerican Express,americanexpress.com\nHoneywell,honeywell.com\n")
    seeds = load_seeds(p)
    assert seeds == [
        Seed(name="American Express", domain="americanexpress.com"),
        Seed(name="Honeywell", domain="honeywell.com"),
    ]


def test_load_seeds_rejects_malformed_row_with_line_number(tmp_path):
    p = tmp_path / "seeds.csv"
    p.write_text("name,domain\nGood Co,good.com\nBad Co,\n")
    with pytest.raises(ValueError, match="line 3"):
        load_seeds(p)


def test_load_seeds_rejects_scheme_in_domain(tmp_path):
    p = tmp_path / "seeds.csv"
    p.write_text("name,domain\nBad Co,https://bad.com\n")
    with pytest.raises(ValueError, match="line 2"):
        load_seeds(p)


def test_checked_in_seed_csv_is_valid_and_substantial():
    seeds = load_seeds(SEED_CSV)
    assert len(seeds) >= 400
    domains = [s.domain for s in seeds]
    assert len(domains) == len(set(domains)), "duplicate domains in seed CSV"
    assert all("." in d and "/" not in d and not d.startswith("http") for d in domains)


from src.fingerprint import detect_unsupported, parse_ats_url


def test_parse_workday_triple_from_url():
    fam, ident = parse_ats_url("https://toyota.wd503.myworkdayjobs.com/TMNA")
    assert fam == "workday"
    assert ident == {"tenant": "toyota", "region": "wd503", "site": "TMNA"}


def test_parse_workday_triple_skips_locale_segment():
    fam, ident = parse_ats_url(
        "https://thomsonreuters.wd5.myworkdayjobs.com/en-US/External_Career_Site/job/USA-MSP/x_JR123"
    )
    assert fam == "workday"
    assert ident == {"tenant": "thomsonreuters", "region": "wd5", "site": "External_Career_Site"}


def test_parse_workday_without_site_returns_none():
    assert parse_ats_url("https://acme.wd1.myworkdayjobs.com/") is None
    assert parse_ats_url("https://acme.wd1.myworkdayjobs.com/en-US/") is None


def test_parse_oraclecloud_triple_from_url():
    fam, ident = parse_ats_url(
        "https://egug.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/requisitions"
    )
    assert fam == "oraclecloud"
    assert ident == {"tenant": "egug", "region": "us2", "site": "CX_1"}


def test_parse_slug_families():
    assert parse_ats_url("https://boards.greenhouse.io/stripe") == ("greenhouse", {"slug": "stripe"})
    assert parse_ats_url("https://job-boards.greenhouse.io/gitlab/jobs/1") == ("greenhouse", {"slug": "gitlab"})
    assert parse_ats_url("https://jobs.lever.co/netflix") == ("lever", {"slug": "netflix"})
    assert parse_ats_url("https://jobs.ashbyhq.com/linear") == ("ashby", {"slug": "linear"})
    assert parse_ats_url("https://apply.workable.com/acme-co/") == ("workable", {"slug": "acme-co"})
    assert parse_ats_url("https://careers.smartrecruiters.com/TheNielsenCompany") == (
        "smartrecruiters", {"slug": "TheNielsenCompany"})
    assert parse_ats_url("https://ats.rippling.com/acme/jobs") == ("rippling", {"slug": "acme"})


def test_parse_rejects_non_ats_and_bare_hosts():
    assert parse_ats_url("https://www.example.com/careers") is None
    assert parse_ats_url("https://boards.greenhouse.io/") is None
    assert parse_ats_url("not-a-url") is None


def test_detect_unsupported_families():
    assert detect_unsupported("https://careers-charter.icims.com/jobs/search?ss=1") == "icims"
    assert detect_unsupported("https://bostonscientific.eightfold.ai/careers") == "eightfold"
    assert detect_unsupported("https://xyz.taleo.net/careersection/ex/jobsearch.ftl") == "taleo"
    assert detect_unsupported("https://career4.successfactors.com/portalcareer") == "successfactors"
    assert detect_unsupported("https://metlife.avature.net/careers") == "avature"
    assert detect_unsupported("https://careers.phenompeople.com/x") == "phenom"
    assert detect_unsupported("https://tbcdn.talentbrew.com/company/27600/x") == "talentbrew"
    assert detect_unsupported("https://www.example.com/") is None


import httpx
import respx

from src.fingerprint import locate_careers_urls


@pytest.mark.asyncio
async def test_locate_returns_final_redirect_urls():
    with respx.mock:
        respx.get("https://acme.com/careers").respond(
            302, headers={"Location": "https://acme.wd5.myworkdayjobs.com/External"})
        respx.get("https://acme.wd5.myworkdayjobs.com/External").respond(200, text="<html/>")
        respx.get("https://acme.com/jobs").respond(404)
        respx.get("https://careers.acme.com").respond(404)
        respx.get("https://acme.com").respond(200, text="<html><body></body></html>")
        async with httpx.AsyncClient(follow_redirects=True) as client:
            urls = await locate_careers_urls(client, "acme.com")
    assert "https://acme.wd5.myworkdayjobs.com/External" in urls


@pytest.mark.asyncio
async def test_locate_extracts_careers_links_from_homepage():
    html = """<html><body>
      <a href="/about">About</a>
      <a href="https://boards.greenhouse.io/acme">Join our team</a>
      <a href="/careers/openings">Careers</a>
    </body></html>"""
    with respx.mock:
        respx.get("https://acme.com/careers").respond(404)
        respx.get("https://acme.com/jobs").respond(404)
        respx.get("https://careers.acme.com").respond(404)
        respx.get("https://acme.com").respond(200, text=html)
        async with httpx.AsyncClient(follow_redirects=True) as client:
            urls = await locate_careers_urls(client, "acme.com")
    assert "https://boards.greenhouse.io/acme" in urls
    assert "https://acme.com/careers/openings" in urls


@pytest.mark.asyncio
async def test_locate_survives_total_failure():
    with respx.mock:
        respx.get(url__regex=r".*").mock(side_effect=httpx.ConnectError("down"))
        async with httpx.AsyncClient(follow_redirects=True) as client:
            urls = await locate_careers_urls(client, "down.com")
    assert urls == []


from src.fingerprint import FingerprintResult, Seed, fingerprint_company, fingerprint_many

_WD_JOBS = {"total": 2, "jobPostings": [
    {"title": "SWE", "externalPath": "/job/x_JR1", "locationsText": "Remote", "bulletFields": ["JR1"]},
    {"title": "PM", "externalPath": "/job/x_JR2", "locationsText": "NYC", "bulletFields": ["JR2"]},
]}


@pytest.mark.asyncio
async def test_fingerprint_matched_workday():
    with respx.mock:
        respx.get("https://acme.com/careers").respond(
            302, headers={"Location": "https://acme.wd5.myworkdayjobs.com/External"})
        respx.get("https://acme.wd5.myworkdayjobs.com/External").respond(200, text="<html/>")
        respx.get("https://acme.com/jobs").respond(404)
        respx.get("https://careers.acme.com").respond(404)
        respx.get("https://acme.com").respond(200, text="<html/>")
        respx.post("https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/External/jobs").respond(200, json=_WD_JOBS)
        async with httpx.AsyncClient(follow_redirects=True) as client:
            r = await fingerprint_company(client, Seed("Acme Corp", "acme.com"))
    assert r.status == "matched"
    assert r.family == "workday"
    assert r.identity == {"tenant": "acme", "region": "wd5", "site": "External"}
    assert r.posting_count == 2
    assert r.name == "Acme Corp"


@pytest.mark.asyncio
async def test_fingerprint_unsupported_only():
    # avature.net (unlike icims.com, as of Task 6) has no jsonld auto-detect
    # branch, so it stays a report-only "unsupported" family.
    with respx.mock:
        respx.get("https://walled.com/careers").respond(
            302, headers={"Location": "https://walled.avature.net/careers"})
        respx.get("https://walled.avature.net/careers").respond(200, text="<html/>")
        respx.get("https://walled.com/jobs").respond(404)
        respx.get("https://careers.walled.com").respond(404)
        respx.get("https://walled.com").respond(200, text="<html/>")
        async with httpx.AsyncClient(follow_redirects=True) as client:
            r = await fingerprint_company(client, Seed("Walled Co", "walled.com"))
    assert r.status == "unsupported"
    assert r.family == "avature"
    assert "avature" in (r.evidence_url or "")


@pytest.mark.asyncio
async def test_fingerprint_verified_but_empty_is_not_found():
    with respx.mock:
        respx.get("https://quiet.com/careers").respond(
            302, headers={"Location": "https://jobs.lever.co/quietco"})
        respx.get("https://jobs.lever.co/quietco").respond(200, text="<html/>")
        respx.get("https://quiet.com/jobs").respond(404)
        respx.get("https://careers.quiet.com").respond(404)
        respx.get("https://quiet.com").respond(200, text="<html/>")
        respx.get("https://api.lever.co/v0/postings/quietco").respond(200, json=[])
        async with httpx.AsyncClient(follow_redirects=True) as client:
            r = await fingerprint_company(client, Seed("Quiet Co", "quiet.com"))
    assert r.status == "not_found"
    assert "empty" in (r.note or "")


@pytest.mark.asyncio
async def test_fingerprint_nothing_detected_is_not_found():
    with respx.mock:
        respx.get(url__regex=r"https://plain\.com.*").respond(200, text="<html><body>hi</body></html>")
        respx.get("https://careers.plain.com").respond(404)
        async with httpx.AsyncClient(follow_redirects=True) as client:
            r = await fingerprint_company(client, Seed("Plain Co", "plain.com"))
    assert r.status == "not_found"


@pytest.mark.asyncio
async def test_fingerprint_many_is_fail_soft_per_company():
    seeds = [Seed("A", "a.com"), Seed("B", "b.com")]
    with respx.mock:
        respx.get(url__regex=r".*a\.com.*").mock(side_effect=httpx.ConnectError("down"))
        respx.get(url__regex=r".*careers\.a\.com.*").mock(side_effect=httpx.ConnectError("down"))
        respx.get(url__regex=r".*b\.com.*").respond(200, text="<html/>")
        respx.get("https://careers.b.com").respond(404)
        results = await fingerprint_many(seeds, concurrency=2)
    by_name = {r.name: r for r in results}
    assert by_name["A"].status in ("not_found", "error")   # all-candidates-dead is honest either way
    assert by_name["B"].status == "not_found"
    assert len(results) == 2


import yaml as _yaml

from src.fingerprint import config_entry_lines, merge_results_into_config

_MINI_CONFIG = """filters:
  titles:
  - software engineer
sources:
  greenhouse:
  - stripe            # existing, with comment
  lever: []
  workday:
  - tenant: salesforce
    region: wd12
    site: External_Career_Site
  oraclecloud:
  - tenant: egug             # American Express
    region: us2
    site: CX_1
    company: American Express
  hn_who_is_hiring:
    enabled: true
schedules:
  ats_minutes: 1
"""


def _matched(name, domain, family, identity, count=5):
    return FingerprintResult(name=name, domain=domain, status="matched",
                             family=family, identity=identity, posting_count=count)


def test_merge_appends_preserving_comments(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(_MINI_CONFIG)
    results = [
        _matched("Acme Corp", "acme.com", "workday", {"tenant": "acme", "region": "wd5", "site": "External"}),
        _matched("Big Oil", "bigoil.com", "oraclecloud", {"tenant": "zzz", "region": "us6", "site": "CX_2"}),
        _matched("Widget Co", "widget.com", "greenhouse", {"slug": "widgetco"}),
    ]
    added, diff = merge_results_into_config(results, config_path=p, already_polled=set())
    assert added == 3
    text = p.read_text()
    assert "# existing, with comment" in text            # comments preserved
    cfg = _yaml.safe_load(text)
    assert {"tenant": "acme", "region": "wd5", "site": "External"} in [
        {k: v for k, v in e.items() if k in ("tenant", "region", "site")} for e in cfg["sources"]["workday"]]
    orc = cfg["sources"]["oraclecloud"]
    assert any(e["tenant"] == "zzz" and e.get("company") == "Big Oil" for e in orc)
    assert "widgetco" in cfg["sources"]["greenhouse"]
    assert cfg["sources"]["hn_who_is_hiring"]["enabled"] is True  # neighbors untouched
    assert "+" in diff and "acme" in diff


def test_merge_dedupes_against_already_polled(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(_MINI_CONFIG)
    results = [_matched("Stripe", "stripe.com", "greenhouse", {"slug": "stripe"})]
    added, _ = merge_results_into_config(results, config_path=p, already_polled={"greenhouse:stripe"})
    assert added == 0
    assert p.read_text() == _MINI_CONFIG                  # untouched


def test_merge_expands_empty_list_family(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(_MINI_CONFIG)
    results = [_matched("Lever Co", "leverco.com", "lever", {"slug": "leverco"})]
    added, _ = merge_results_into_config(results, config_path=p, already_polled=set())
    assert added == 1
    cfg = _yaml.safe_load(p.read_text())
    assert cfg["sources"]["lever"] == ["leverco"]


def test_merge_skips_non_matched(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(_MINI_CONFIG)
    results = [FingerprintResult(name="X", domain="x.com", status="unsupported", family="icims")]
    added, _ = merge_results_into_config(results, config_path=p, already_polled=set())
    assert added == 0


def test_merge_appends_into_commented_empty_list_family(tmp_path):
    # Reviewer-reproduced bug: a family line carrying a trailing inline
    # comment (`lever: []  # keep empty until vetted`) used to fail the
    # family-detection regex, causing a SECOND `lever:` key to be inserted
    # elsewhere — silently shadowing the original block (with its comment).
    config = _MINI_CONFIG.replace("  lever: []", "  lever: []  # keep empty until vetted")
    p = tmp_path / "config.yaml"
    p.write_text(config)
    results = [_matched("Lever Co", "leverco.com", "lever", {"slug": "leverco"})]
    added, _ = merge_results_into_config(results, config_path=p, already_polled=set())
    assert added == 1
    text = p.read_text()
    assert text.count("  lever:") == 1                    # no duplicate key inserted
    assert "# keep empty until vetted" in text             # comment preserved
    cfg = _yaml.safe_load(text)
    assert cfg["sources"]["lever"] == ["leverco"]


def test_merge_structural_check_restores_on_shadowing(tmp_path, monkeypatch):
    # Simulate the pre-fix bug directly: _insert_into_family always treats the
    # family as missing and inserts a SECOND `greenhouse:` key inside
    # `sources:`, producing a duplicate mapping key. PyYAML resolves duplicate
    # keys via last-wins, so this either drops the original entry or the new
    # one — the structural post-check must catch it, raise, and restore the
    # file untouched.
    import src.fingerprint as fingerprint

    def fake_insert(lines, family, entry_lines):
        out = list(lines)
        anchor = next(i for i, line in enumerate(out) if line.startswith("sources:"))
        insert_at = anchor + 1
        return out[:insert_at] + [f"  {family}:"] + entry_lines + out[insert_at:]

    monkeypatch.setattr(fingerprint, "_insert_into_family", fake_insert)

    p = tmp_path / "config.yaml"
    p.write_text(_MINI_CONFIG)
    results = [_matched("Widget Co", "widget.com", "greenhouse", {"slug": "widgetco"})]
    with pytest.raises(ValueError, match="greenhouse"):
        merge_results_into_config(results, config_path=p, already_polled=set())
    assert p.read_text() == _MINI_CONFIG                   # restored, byte-for-byte


# ---------------------------------------------------------------------------
# Final-review fixes: scan fetched bodies for ATS links, jobs.{domain} probe,
# bare-locale tolerance, within-run merge dedupe, homepage short-circuit.
# ---------------------------------------------------------------------------

_WD_JOBS_ONE = {"total": 1, "jobPostings": [
    {"title": "Engineer", "externalPath": "/job/x_JR9", "locationsText": "Remote", "bulletFields": ["JR9"]},
]}


@pytest.mark.asyncio
async def test_locate_scans_fetched_body_for_ats_links():
    # 3M-shaped: the well-known probe 200s directly (no redirect) with a
    # landing page whose real Workday board is only reachable via a link in
    # the body — that link must not be discarded.
    body = ('<html><body><a href="https://acme.wd1.myworkdayjobs.com/en-US/AcmeCareers/job/x">'
            'Search jobs</a></body></html>')
    with respx.mock:
        respx.get("https://acme.com/careers").respond(200, text=body)
        respx.get("https://acme.com/jobs").respond(404)
        respx.get("https://careers.acme.com").respond(404)
        respx.get("https://jobs.acme.com").respond(404)
        async with httpx.AsyncClient(follow_redirects=True) as client:
            urls = await locate_careers_urls(client, "acme.com")
    assert "https://acme.wd1.myworkdayjobs.com/en-US/AcmeCareers/job/x" in urls


@pytest.mark.asyncio
async def test_locate_body_scan_skips_malformed_hrefs():
    # A body containing both a malformed href (invalid URL syntax) and a
    # valid ATS link must skip the malformed one and extract the valid one.
    body = ('<html><body>'
            '<a href="https://[::1]:badport/">bad</a>'
            '<a href="https://jobs.lever.co/acme">jobs</a>'
            '</body></html>')
    with respx.mock:
        respx.get("https://acme.com/careers").respond(200, text=body)
        respx.get("https://acme.com/jobs").respond(404)
        respx.get("https://careers.acme.com").respond(404)
        respx.get("https://jobs.acme.com").respond(404)
        async with httpx.AsyncClient(follow_redirects=True) as client:
            urls = await locate_careers_urls(client, "acme.com")
    assert "https://jobs.lever.co/acme" in urls


@pytest.mark.asyncio
async def test_fingerprint_matched_via_body_scanned_workday_link():
    body = ('<html><body><a href="https://acme.wd1.myworkdayjobs.com/en-US/AcmeCareers/job/x">'
            'Search jobs</a></body></html>')
    with respx.mock:
        respx.get("https://acme.com/careers").respond(200, text=body)
        respx.get("https://acme.com/jobs").respond(404)
        respx.get("https://careers.acme.com").respond(404)
        respx.get("https://jobs.acme.com").respond(404)
        respx.post("https://acme.wd1.myworkdayjobs.com/wday/cxs/acme/AcmeCareers/jobs").respond(
            200, json=_WD_JOBS_ONE)
        async with httpx.AsyncClient(follow_redirects=True) as client:
            r = await fingerprint_company(client, Seed("Acme Corp", "acme.com"))
    assert r.status == "matched"
    assert r.family == "workday"
    assert r.identity == {"tenant": "acme", "region": "wd1", "site": "AcmeCareers"}


@pytest.mark.asyncio
async def test_locate_probes_jobs_subdomain():
    with respx.mock:
        respx.get("https://acme.com/careers").respond(404)
        respx.get("https://acme.com/jobs").respond(404)
        respx.get("https://careers.acme.com").respond(404)
        respx.get("https://jobs.acme.com").respond(
            302, headers={"Location": "https://acme-careers.icims.com/jobs/intro"})
        respx.get("https://acme-careers.icims.com/jobs/intro").respond(200, text="<html/>")
        async with httpx.AsyncClient(follow_redirects=True) as client:
            urls = await locate_careers_urls(client, "acme.com")
    assert "https://acme-careers.icims.com/jobs/intro" in urls


@pytest.mark.asyncio
async def test_fingerprint_unsupported_via_jobs_subdomain_probe():
    # avature.net (unlike icims.com, as of Task 6) has no jsonld auto-detect
    # branch, so it stays a report-only "unsupported" family.
    with respx.mock:
        respx.get("https://acme.com/careers").respond(404)
        respx.get("https://acme.com/jobs").respond(404)
        respx.get("https://careers.acme.com").respond(404)
        respx.get("https://jobs.acme.com").respond(
            302, headers={"Location": "https://acme.avature.net/careers"})
        respx.get("https://acme.avature.net/careers").respond(200, text="<html/>")
        async with httpx.AsyncClient(follow_redirects=True) as client:
            r = await fingerprint_company(client, Seed("Acme Corp", "acme.com"))
    assert r.status == "unsupported"
    assert r.family == "avature"


def test_parse_workday_tolerates_bare_two_letter_locale():
    fam, ident = parse_ats_url("https://acme.wd1.myworkdayjobs.com/es/Search")
    assert fam == "workday"
    assert ident == {"tenant": "acme", "region": "wd1", "site": "Search"}
    # A bare two-letter segment with nothing after it must not become the site.
    assert parse_ats_url("https://acme.wd1.myworkdayjobs.com/es") is None
    # Existing en-US-style locale handling is unaffected.
    fam2, ident2 = parse_ats_url("https://acme.wd1.myworkdayjobs.com/en-US/External")
    assert fam2 == "workday"
    assert ident2["site"] == "External"


def test_merge_dedupes_within_same_run(tmp_path):
    # Two seeds resolving to the same board must only insert once, and the
    # caller's already_polled set must not be mutated as a side effect.
    p = tmp_path / "config.yaml"
    p.write_text(_MINI_CONFIG)
    results = [
        _matched("Acme Corp", "acme.com", "greenhouse", {"slug": "acmeco"}),
        _matched("Acme Corp (alt seed)", "acme-corp.com", "greenhouse", {"slug": "acmeco"}),
    ]
    caller_set = set()
    added, _ = merge_results_into_config(results, config_path=p, already_polled=caller_set)
    assert added == 1
    cfg = _yaml.safe_load(p.read_text())
    assert cfg["sources"]["greenhouse"].count("acmeco") == 1
    assert caller_set == set()                              # caller's set untouched


@pytest.mark.asyncio
async def test_locate_skips_homepage_fetch_when_already_fingerprintable():
    body = '<html><body><a href="https://boards.greenhouse.io/acme">Team</a></body></html>'
    with respx.mock:
        respx.get("https://acme.com/careers").respond(200, text=body)
        respx.get("https://acme.com/jobs").respond(404)
        respx.get("https://careers.acme.com").respond(404)
        respx.get("https://jobs.acme.com").respond(404)
        homepage_route = respx.get("https://acme.com").respond(200, text="<html/>")
        async with httpx.AsyncClient(follow_redirects=True) as client:
            urls = await locate_careers_urls(client, "acme.com")
    assert "https://boards.greenhouse.io/acme" in urls
    assert homepage_route.call_count == 0


# ---------------------------------------------------------------------------
# Task 6: jsonld (iCIMS auto-detect) fingerprint probe + jsonld_boards merge.
# ---------------------------------------------------------------------------

from src.fingerprint import parse_jsonld_url, connector_name, config_entry_lines, FingerprintResult


def test_parse_jsonld_url_icims():
    fam, ident = parse_jsonld_url("https://careers-steeldynamics.icims.com/jobs/search")
    assert fam == "jsonld"
    assert ident["family"] == "icims"
    assert ident["slug"] == "steeldynamics"
    assert ident["base_url"] == "https://careers-steeldynamics.icims.com"


def test_parse_jsonld_url_branded_domain_returns_none():
    # SF/TalentBrew branded domains aren't host-classifiable — hand-curated.
    assert parse_jsonld_url("https://jobs.aosmith.com/jobsfeed.xml") is None


def test_parse_jsonld_url_non_family_returns_none():
    assert parse_jsonld_url("https://boards.greenhouse.io/stripe") is None


def test_connector_name_jsonld():
    r = FingerprintResult(name="Steel Dynamics", domain="steeldynamics.com", status="matched",
                          family="jsonld",
                          identity={"family": "icims", "slug": "steeldynamics",
                                    "base_url": "https://careers-steeldynamics.icims.com"})
    assert connector_name(r) == "icims:steeldynamics"


def test_config_entry_lines_jsonld():
    r = FingerprintResult(name="Steel Dynamics", domain="steeldynamics.com", status="matched",
                          family="jsonld",
                          identity={"family": "icims", "slug": "steeldynamics",
                                    "base_url": "https://careers-steeldynamics.icims.com"})
    lines = config_entry_lines(r)
    body = "\n".join(lines)
    assert "family: icims" in body
    assert "slug: steeldynamics" in body
    assert "base_url: https://careers-steeldynamics.icims.com" in body
    assert "company: Steel Dynamics" in body


@pytest.mark.asyncio
async def test_fingerprint_matched_icims():
    with respx.mock:
        respx.get("https://steeldynamics.com/careers").respond(
            302, headers={"Location": "https://careers-steeldynamics.icims.com/jobs/intro"})
        respx.get("https://careers-steeldynamics.icims.com/jobs/intro").respond(200, text="<html/>")
        respx.get("https://steeldynamics.com/jobs").respond(404)
        respx.get("https://careers.steeldynamics.com").respond(404)
        respx.get("https://steeldynamics.com").respond(200, text="<html/>")
        card = ('<li class="iCIMS_JobCardItem">'
                '<h3>Welder</h3><div class="title"><a href="/jobs/123/welder/job">x</a></div>'
                '<div class="header left"><span>Butler, IN</span></div></li>')
        respx.get(url__regex=r"https://careers-steeldynamics\.icims\.com/jobs/search\?ss=1&in_iframe=1&pr=0&.*").respond(
            200, text=f"<html><body><ul>{card}</ul></body></html>")
        respx.get(url__regex=r"https://careers-steeldynamics\.icims\.com/jobs/search\?.*").respond(
            200, text="<html><body></body></html>")
        async with httpx.AsyncClient(follow_redirects=True) as client:
            r = await fingerprint_company(client, Seed("Steel Dynamics", "steeldynamics.com"))
    assert r.status == "matched"
    assert r.family == "jsonld"
    assert r.identity == {"family": "icims", "slug": "steeldynamics",
                           "base_url": "https://careers-steeldynamics.icims.com"}
    assert r.posting_count == 1
    assert connector_name(r) == "icims:steeldynamics"


@pytest.mark.asyncio
async def test_fingerprint_matched_icims_absorbs_second_same_host_url():
    # A real careers page redirects into the iCIMS tenant, and that landing
    # page's own body has a nav/login link back into the same iCIMS host on a
    # different path — the body scan in locate_careers_urls surfaces both.
    # The second URL must be absorbed into the existing jsonld/icims match,
    # not fall through to detect_unsupported and report "unsupported".
    with respx.mock:
        respx.get("https://steeldynamics.com/careers").respond(
            302, headers={"Location": "https://careers-steeldynamics.icims.com/jobs/intro"})
        respx.get("https://careers-steeldynamics.icims.com/jobs/intro").respond(
            200, text='<html><body><a href="/jobs/login">Login</a></body></html>')
        respx.get("https://steeldynamics.com/jobs").respond(404)
        respx.get("https://careers.steeldynamics.com").respond(404)
        card = ('<li class="iCIMS_JobCardItem">'
                '<h3>Welder</h3><div class="title"><a href="/jobs/123/welder/job">x</a></div>'
                '<div class="header left"><span>Butler, IN</span></div></li>')
        respx.get(url__regex=r"https://careers-steeldynamics\.icims\.com/jobs/search\?ss=1&in_iframe=1&pr=0&.*").respond(
            200, text=f"<html><body><ul>{card}</ul></body></html>")
        respx.get(url__regex=r"https://careers-steeldynamics\.icims\.com/jobs/search\?.*").respond(
            200, text="<html><body></body></html>")
        async with httpx.AsyncClient(follow_redirects=True) as client:
            urls = await locate_careers_urls(client, "steeldynamics.com")
            r = await fingerprint_company(client, Seed("Steel Dynamics", "steeldynamics.com"))
    # Confirm the fixture really did surface two distinct iCIMS URLs on the
    # same host — otherwise this test wouldn't exercise the bug at all.
    icims_urls = [u for u in urls if "careers-steeldynamics.icims.com" in u]
    assert len(icims_urls) >= 2
    assert r.status == "matched"
    assert r.family == "jsonld"
    assert r.identity["family"] == "icims"
    assert r.identity["slug"] == "steeldynamics"
    assert connector_name(r) == "icims:steeldynamics"


@pytest.mark.asyncio
async def test_fingerprint_walled_icims_is_not_found():
    # An IP-walled iCIMS tenant fingerprints as jsonld/icims, but the live
    # probe sees zero job cards -> falls through to not_found (not
    # "unsupported") so the CLI never emits a dead board.
    with respx.mock:
        respx.get("https://walled.com/careers").respond(
            302, headers={"Location": "https://careers-walled.icims.com/jobs/intro"})
        respx.get("https://careers-walled.icims.com/jobs/intro").respond(200, text="<html/>")
        respx.get("https://walled.com/jobs").respond(404)
        respx.get("https://careers.walled.com").respond(404)
        respx.get("https://walled.com").respond(200, text="<html/>")
        respx.get(url__regex=r"https://careers-walled\.icims\.com/jobs/search\?.*").respond(
            200, text="<html><body>no jobs here</body></html>")
        async with httpx.AsyncClient(follow_redirects=True) as client:
            r = await fingerprint_company(client, Seed("Walled Co", "walled.com"))
    assert r.status == "not_found"
    assert r.family == "jsonld"
    assert r.identity == {"family": "icims", "slug": "walled",
                           "base_url": "https://careers-walled.icims.com"}


def test_merge_writes_jsonld_board_entry(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(_MINI_CONFIG)
    r = FingerprintResult(
        name="Steel Dynamics", domain="steeldynamics.com", status="matched", family="jsonld",
        identity={"family": "icims", "slug": "steeldynamics",
                  "base_url": "https://careers-steeldynamics.icims.com"},
        posting_count=40,
    )
    added, diff = merge_results_into_config([r], config_path=p, already_polled=set())
    assert added == 1
    cfg = _yaml.safe_load(p.read_text())
    boards = cfg["sources"]["jsonld_boards"]
    assert any(b["family"] == "icims" and b["slug"] == "steeldynamics"
               and b["base_url"] == "https://careers-steeldynamics.icims.com"
               and b["company"] == "Steel Dynamics"
               for b in boards)
    # existing entries preserved (structural post-check would abort otherwise)
    assert cfg["sources"]["greenhouse"] == ["stripe"]
    assert cfg["sources"]["oraclecloud"][0]["tenant"] == "egug"
    assert "jsonld_boards" in diff


def test_merge_dedupes_jsonld_board_against_already_polled(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(_MINI_CONFIG)
    r = FingerprintResult(
        name="Steel Dynamics", domain="steeldynamics.com", status="matched", family="jsonld",
        identity={"family": "icims", "slug": "steeldynamics",
                  "base_url": "https://careers-steeldynamics.icims.com"},
        posting_count=40,
    )
    added, _ = merge_results_into_config([r], config_path=p, already_polled={"icims:steeldynamics"})
    assert added == 0
    assert p.read_text() == _MINI_CONFIG


# ---------------------------------------------------------------------------
# Task 3 (eightfold branch): fingerprint auto-detect (domain scrape + flavor
# probe) + merge into sources.eightfold.
# ---------------------------------------------------------------------------

from src.fingerprint import (
    parse_eightfold_url, connector_name, config_entry_lines, FingerprintResult,
)


def test_parse_eightfold_url():
    fam, ident = parse_eightfold_url("https://bms.eightfold.ai/careers")
    assert fam == "eightfold"
    assert ident["slug"] == "bms"
    assert ident["base"] == "https://bms.eightfold.ai"


def test_parse_eightfold_url_non_family_returns_none():
    assert parse_eightfold_url("https://boards.greenhouse.io/stripe") is None
    assert parse_eightfold_url("https://careers-x.icims.com/jobs/search") is None


def test_connector_name_eightfold():
    r = FingerprintResult(name="Bristol Myers Squibb", domain="bms.com", status="matched",
                          family="eightfold",
                          identity={"slug": "bms", "domain": "bms.com", "flavor": "pcsx"})
    assert connector_name(r) == "eightfold:bms"


def test_config_entry_lines_eightfold():
    r = FingerprintResult(name="Bristol Myers Squibb", domain="bms.com", status="matched",
                          family="eightfold",
                          identity={"slug": "bms", "domain": "bms.com", "flavor": "pcsx"})
    body = "\n".join(config_entry_lines(r))
    assert "slug: bms" in body
    assert "domain: bms.com" in body
    assert "flavor: pcsx" in body
    assert "company: Bristol Myers Squibb" in body


def test_merge_writes_eightfold_entry(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("sources:\n  greenhouse: [stripe]\n")
    r = FingerprintResult(name="Bristol Myers Squibb", domain="bms.com", status="matched",
                          family="eightfold",
                          identity={"slug": "bms", "domain": "bms.com", "flavor": "pcsx"},
                          posting_count=40)
    added, _diff = merge_results_into_config([r], config_path=cfg, already_polled=set())
    assert added == 1
    loaded = _yaml.safe_load(cfg.read_text())
    assert any(e["slug"] == "bms" and e["domain"] == "bms.com" and e["flavor"] == "pcsx"
               for e in loaded["sources"]["eightfold"])
    assert loaded["sources"]["greenhouse"] == ["stripe"]


@pytest.mark.asyncio
async def test_fingerprint_matched_eightfold_pcsx():
    # The board's /careers page embeds a `domain=` config param (this is how
    # the live Eightfold board tells its own JS client which employer's
    # postings to fetch) — the fingerprinter scrapes it live since it can't
    # be derived from the .eightfold.ai host alone.
    with respx.mock:
        respx.get("https://bms.com/careers").respond(
            302, headers={"Location": "https://bms.eightfold.ai/careers"})
        respx.get("https://bms.eightfold.ai/careers").respond(
            200, text='<html><body><script>window.domain=bms.com;</script></body></html>')
        respx.get("https://bms.com/jobs").respond(404)
        respx.get("https://careers.bms.com").respond(404)
        respx.get("https://jobs.bms.com").respond(404)
        respx.get(url__regex=r"https://bms\.eightfold\.ai/api/pcsx/search\?.*").respond(
            200, json={"data": {"positions": [
                {"id": "1", "name": "Software Engineer", "positionUrl": "/careers/job/1",
                 "locations": ["New York, NY"]},
            ], "count": 1}})
        async with httpx.AsyncClient(follow_redirects=True) as client:
            r = await fingerprint_company(client, Seed("Bristol Myers Squibb", "bms.com"))
    assert r.status == "matched"
    assert r.family == "eightfold"
    assert r.identity["slug"] == "bms"
    assert r.identity["base"] == "https://bms.eightfold.ai"
    assert r.identity["domain"] == "bms.com"
    assert r.identity["flavor"] == "pcsx"
    assert r.posting_count == 1
    assert r.evidence_url == "https://bms.eightfold.ai/careers"
    assert connector_name(r) == "eightfold:bms"


@pytest.mark.asyncio
async def test_fingerprint_matched_eightfold_falls_back_to_apply_v2():
    # pcsx probes clean but empty (this tenant runs the other product
    # generation) — the verifier must try apply_v2 next and record whichever
    # flavor actually returned postings.
    with respx.mock:
        respx.get("https://acme.com/careers").respond(
            302, headers={"Location": "https://acme.eightfold.ai/careers"})
        respx.get("https://acme.eightfold.ai/careers").respond(
            200, text='<html><body><script>window.domain=acme.com;</script></body></html>')
        respx.get("https://acme.com/jobs").respond(404)
        respx.get("https://careers.acme.com").respond(404)
        respx.get("https://jobs.acme.com").respond(404)
        respx.get(url__regex=r"https://acme\.eightfold\.ai/api/pcsx/search\?.*").respond(
            200, json={"data": {"positions": [], "count": 0}})
        respx.get(url__regex=r"https://acme\.eightfold\.ai/api/apply/v2/jobs\?.*").respond(
            200, json={"positions": [
                {"id": "9", "name": "Data Scientist", "positionUrl": "/careers/job/9"},
            ], "count": 1})
        async with httpx.AsyncClient(follow_redirects=True) as client:
            r = await fingerprint_company(client, Seed("Acme Corp", "acme.com"))
    assert r.status == "matched"
    assert r.family == "eightfold"
    assert r.identity["flavor"] == "apply_v2"
    assert r.posting_count == 1


@pytest.mark.asyncio
async def test_fingerprint_matched_eightfold_probes_flavor_directly_for_apply_v2_tenant():
    # A real apply_v2 tenant: pcsx returns a genuine 403 flavor-error. The
    # probe must hit conn._fetch_flavor() directly instead of conn.fetch() —
    # fetch() has its own internal 403->apply_v2 fallback, so probing pcsx
    # through fetch() would silently succeed via that fallback and record the
    # WRONG flavor ("pcsx") even though this tenant only serves apply_v2.
    # _fetch_flavor() has no such fallback: it raises _FlavorMismatch on the
    # 403, the probe loop's `except Exception: continue` absorbs it, and the
    # loop advances to apply_v2 — recording the correct flavor.
    with respx.mock:
        respx.get("https://albemarle.com/careers").respond(
            302, headers={"Location": "https://albemarle.eightfold.ai/careers"})
        respx.get("https://albemarle.eightfold.ai/careers").respond(
            200, text='<html><body><script>window.domain=albemarle.com;</script></body></html>')
        respx.get("https://albemarle.com/jobs").respond(404)
        respx.get("https://careers.albemarle.com").respond(404)
        respx.get("https://jobs.albemarle.com").respond(404)
        respx.get(url__regex=r"https://albemarle\.eightfold\.ai/api/pcsx/search\?.*").respond(
            403, json={"message": "PCSX is not enabled for this user."})
        respx.get(url__regex=r"https://albemarle\.eightfold\.ai/api/apply/v2/jobs\?.*").respond(
            200, json={"positions": [
                {"id": "42", "name": "Process Engineer", "positionUrl": "/careers/job/42"},
            ], "count": 1})
        async with httpx.AsyncClient(follow_redirects=True) as client:
            r = await fingerprint_company(client, Seed("Albemarle", "albemarle.com"))
    assert r.status == "matched"
    assert r.family == "eightfold"
    assert r.identity["flavor"] == "apply_v2"
    assert r.posting_count == 1


@pytest.mark.asyncio
async def test_fingerprint_eightfold_absorbs_second_same_host_url():
    # The eightfold /careers landing page links to another path on the same
    # .eightfold.ai host (e.g. a featured job card) — locate_careers_urls'
    # body scan surfaces both as distinct candidate URLs. The second one must
    # be absorbed into the existing eightfold match, not demoted to
    # "unsupported" via detect_unsupported — the exact bug fixed for iCIMS.
    with respx.mock:
        respx.get("https://bms.com/careers").respond(
            302, headers={"Location": "https://bms.eightfold.ai/careers"})
        respx.get("https://bms.eightfold.ai/careers").respond(
            200, text=('<html><body><script>window.domain=bms.com;</script>'
                       '<a href="/careers/job/456">Featured role</a></body></html>'))
        respx.get("https://bms.com/jobs").respond(404)
        respx.get("https://careers.bms.com").respond(404)
        respx.get("https://jobs.bms.com").respond(404)
        respx.get(url__regex=r"https://bms\.eightfold\.ai/api/pcsx/search\?.*").respond(
            200, json={"data": {"positions": [
                {"id": "1", "name": "Software Engineer", "positionUrl": "/careers/job/1"},
            ], "count": 1}})
        async with httpx.AsyncClient(follow_redirects=True) as client:
            urls = await locate_careers_urls(client, "bms.com")
            r = await fingerprint_company(client, Seed("Bristol Myers Squibb", "bms.com"))
    eightfold_urls = [u for u in urls if "bms.eightfold.ai" in u]
    assert len(eightfold_urls) >= 2  # confirm the fixture exercises the absorb path
    assert r.status == "matched"
    assert r.family == "eightfold"
    assert r.identity["slug"] == "bms"
    assert connector_name(r) == "eightfold:bms"


@pytest.mark.asyncio
async def test_fingerprint_eightfold_no_domain_scrape_is_not_found():
    # A dead-ish eightfold slug (e.g. Amgen's) 200s but its /careers shell has
    # no `domain=` anywhere in the body — the verifier can't resolve a domain
    # to query and must report not_found so the CLI never emits an unusable
    # entry (the "a 404 slug like Amgen is never emitted" case).
    with respx.mock:
        respx.get("https://amgen.com/careers").respond(
            302, headers={"Location": "https://amgen.eightfold.ai/careers"})
        respx.get("https://amgen.eightfold.ai/careers").respond(
            200, text='<html><body><div id="root"></div></body></html>')
        respx.get("https://amgen.com/jobs").respond(404)
        respx.get("https://careers.amgen.com").respond(404)
        respx.get("https://jobs.amgen.com").respond(404)
        async with httpx.AsyncClient(follow_redirects=True) as client:
            r = await fingerprint_company(client, Seed("Amgen", "amgen.com"))
    assert r.status == "not_found"
    assert r.family == "eightfold"
    assert r.identity["slug"] == "amgen"
    assert r.posting_count == 0


from src.fingerprint import parse_taleo_url, connector_name, config_entry_lines, FingerprintResult


def test_parse_taleo_url():
    fam, ident = parse_taleo_url("https://cinfin.taleo.net/careersection/ex/jobsearch.ftl?lang=en")
    assert fam == "taleo"
    assert ident == {"tenant": "cinfin", "section": "ex"}


def test_parse_taleo_url_non_family_returns_none():
    assert parse_taleo_url("https://boards.greenhouse.io/stripe") is None
    assert parse_taleo_url("https://cinfin.taleo.net/") is None  # no /careersection/{section}/


def test_parse_taleo_url_rejects_non_board_sections():
    assert parse_taleo_url("https://cinfin.taleo.net/careersection/iam/accessmanagement/x") is None
    fam, ident = parse_taleo_url("https://cinfin.taleo.net/careersection/ex/jobsearch.ftl?lang=en")
    assert fam == "taleo"
    assert ident == {"tenant": "cinfin", "section": "ex"}


def test_connector_name_taleo():
    r = FingerprintResult(name="Cincinnati Financial", domain="cinfin.com", status="matched",
                          family="taleo", identity={"tenant": "cinfin", "section": "ex"})
    assert connector_name(r) == "taleo:cinfin:ex"


def test_config_entry_lines_taleo():
    r = FingerprintResult(name="Cincinnati Financial", domain="cinfin.com", status="matched",
                          family="taleo", identity={"tenant": "cinfin", "section": "ex"})
    body = "\n".join(config_entry_lines(r))
    assert "tenant: cinfin" in body
    assert 'section: "ex"' in body
    assert "company: Cincinnati Financial" in body


def test_merge_writes_taleo_entry(tmp_path):
    from src.fingerprint import merge_results_into_config
    cfg = tmp_path / "config.yaml"
    cfg.write_text("sources:\n  greenhouse: [stripe]\n")
    r = FingerprintResult(name="Cincinnati Financial", domain="cinfin.com", status="matched",
                          family="taleo", identity={"tenant": "cinfin", "section": "ex"},
                          posting_count=40)
    added, _diff = merge_results_into_config([r], config_path=cfg, already_polled=set())
    assert added == 1
    import yaml
    loaded = yaml.safe_load(cfg.read_text())
    assert any(e["tenant"] == "cinfin" and e["section"] == "ex"
               for e in loaded["sources"]["taleo"])
    assert loaded["sources"]["greenhouse"] == ["stripe"]


def test_merge_quotes_numeric_taleo_section(tmp_path):
    """A tenant whose section is a bare number (e.g. valero's "2") must be
    merged as a YAML string, not an int — else the post-check identity
    comparison mismatches and the merge rolls back, and TaleoBoard(section:
    str) would reject the int at poller boot."""
    from src.fingerprint import merge_results_into_config
    cfg = tmp_path / "config.yaml"
    cfg.write_text("sources:\n  greenhouse: [stripe]\n")
    r = FingerprintResult(name="Valero Energy", domain="valero.com", status="matched",
                          family="taleo", identity={"tenant": "valero", "section": "2"},
                          posting_count=40)
    added, _diff = merge_results_into_config([r], config_path=cfg, already_polled=set())
    assert added == 1
    import yaml
    loaded = yaml.safe_load(cfg.read_text())
    entry = next(e for e in loaded["sources"]["taleo"] if e["tenant"] == "valero")
    assert isinstance(entry["section"], str)
    assert entry["section"] == "2"
    from src.config import TaleoBoard
    TaleoBoard(**entry)  # round-trips without error


def test_gather_already_polled_importable_from_fingerprint(tmp_path):
    from src.fingerprint import gather_already_polled
    p = tmp_path / "config.yaml"
    p.write_text(
        "sources:\n"
        "  greenhouse: [stripe]\n"
        "  taleo:\n"
        "    - {tenant: cinfin, section: ex}\n"
    )
    names = gather_already_polled(p)
    assert "greenhouse:stripe" in names
    assert "taleo:cinfin:ex" in names


from unittest.mock import AsyncMock, patch

from src.fingerprint import verify_identity


@pytest.mark.asyncio
async def test_verify_identity_dispatches_per_family():
    client = object()
    with patch("src.fingerprint._verify_jsonld", new=AsyncMock(return_value=3)) as vj, \
         patch("src.fingerprint._verify_eightfold", new=AsyncMock(return_value=4)) as ve, \
         patch("src.fingerprint._verify_taleo", new=AsyncMock(return_value=5)) as vt, \
         patch("src.fingerprint._verify", new=AsyncMock(return_value=6)) as v:
        assert await verify_identity(client, "jsonld", {"family": "icims"}) == 3
        assert await verify_identity(client, "eightfold", {"slug": "x", "base": "https://x.eightfold.ai"}) == 4
        assert await verify_identity(client, "taleo", {"tenant": "t", "section": "1"}) == 5
        assert await verify_identity(client, "workday", {"tenant": "t", "region": "wd1", "site": "S"}) == 6
    vj.assert_awaited_once()
    ve.assert_awaited_once()
    vt.assert_awaited_once()
    v.assert_awaited_once()


@pytest.mark.parametrize("url,expected", [
    ("https://sendcloud.recruitee.com/o/backend-eng", ("recruitee", {"slug": "sendcloud"})),
    ("https://everphone.jobs.personio.de/job/123", ("personio", {"slug": "everphone"})),
    ("https://snocks.jobs.personio.com/xml", ("personio", {"slug": "snocks"})),
    ("https://tibber.teamtailor.com/jobs/123-x", ("teamtailor", {"slug": "tibber"})),
    # host root with no path still identifies the tenant (slug is in the subdomain)
    ("https://tibber.teamtailor.com/", ("teamtailor", {"slug": "tibber"})),
])
def test_parse_ats_url_eu_subdomain_hosts(url, expected):
    assert parse_ats_url(url) == expected


@pytest.mark.parametrize("url", [
    "https://www.recruitee.com/pricing",       # marketing site, not a tenant
    "https://docs.recruitee.com/reference",    # docs site
    "https://www.teamtailor.com/en/features",  # marketing site
    "https://api.teamtailor.com/v1",           # API host
])
def test_parse_ats_url_reserved_subdomains_rejected(url):
    assert parse_ats_url(url) is None


# ---- EU seed file ----
def test_eu_seeds_load_and_are_wellformed():
    from src.fingerprint import EU_SEEDS, load_seeds
    seeds = load_seeds(EU_SEEDS)
    assert len(seeds) >= 100
    domains = [s.domain for s in seeds]
    assert len(domains) == len(set(domains)), "duplicate domain within eu_companies.csv"


def test_eu_seeds_do_not_overlap_default_seeds():
    from src.fingerprint import DEFAULT_SEEDS, EU_SEEDS, load_seeds
    eu = {s.domain for s in load_seeds(EU_SEEDS)}
    us = {s.domain for s in load_seeds(DEFAULT_SEEDS)}
    assert not (eu & us), f"domains in both seed files would double-sweep: {sorted(eu & us)}"
