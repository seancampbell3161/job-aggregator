import httpx
import pytest
import respx

from scripts.pack_builder import candidates as cand_mod
from scripts.pack_builder.candidates import (
    SOURCE_LEVER, Candidate, candidate_from_url, dedup, identity_key, lever_guesses,
    SOURCE_CC, supplement_companies, yc_us_companies,
)
from src.vc_portfolio import PortfolioCompany
from src.yc_oss import _FEED_URL, YcCompany


@pytest.mark.parametrize("url, family, identity", [
    ("https://boards.greenhouse.io/stripe/jobs/1", "greenhouse", {"slug": "stripe"}),
    ("https://job-boards.greenhouse.io/gitlab", "greenhouse", {"slug": "gitlab"}),
    ("https://boards.greenhouse.io/embed/job_board?for=acme&b=x", "greenhouse", {"slug": "acme"}),
    ("https://jobs.ashbyhq.com/linear/abc", "ashby", {"slug": "linear"}),
    ("https://jobs.lever.co/netflix/123", "lever", {"slug": "netflix"}),
    ("https://jobs.smartrecruiters.com/Visa/7440-swe", "smartrecruiters", {"slug": "Visa"}),
    ("https://apply.workable.com/acme/j/123", "workable", {"slug": "acme"}),
    ("https://ats.rippling.com/acme/jobs", "rippling", {"slug": "acme"}),
    ("https://toyota.wd503.myworkdayjobs.com/en-US/TMNA/job/1", "workday",
     {"tenant": "toyota", "region": "wd503", "site": "TMNA"}),
    ("https://acme.jobs.personio.de/job/1", "personio", {"slug": "acme"}),
    ("https://acme.recruitee.com/o/x", "recruitee", {"slug": "acme"}),
    ("https://acme.teamtailor.com/jobs/1", "teamtailor", {"slug": "acme"}),
    ("https://acme.taleo.net/careersection/2/jobdetail.ftl", "taleo",
     {"tenant": "acme", "section": "2"}),
])
def test_candidate_from_url(url, family, identity):
    c = candidate_from_url(url)
    assert c is not None and c.family == family
    assert {k: v for k, v in c.identity.items() if k in identity} == identity


@pytest.mark.parametrize("url", [
    "https://boards.greenhouse.io/embed/job_board",          # embed without for=
    "https://boards.greenhouse.io/robots.txt",
    "https://jobs.lever.co/acme%20corp/1",                    # encoded junk
    "https://jobs.ashbyhq.com/api/x",                        # reserved path
    "https://[::1/bad",                                       # malformed host
    "https://example.com/careers",                           # not a board host
])
def test_junk_urls_are_skipped(url):
    assert candidate_from_url(url) is None


def test_dedup_is_case_insensitive_and_keeps_first_seen():
    a = Candidate("greenhouse", {"slug": "Stripe"})
    b = Candidate("greenhouse", {"slug": "stripe"}, source=SOURCE_LEVER)
    out = dedup([a, b, Candidate("ashby", {"slug": "x"})])
    assert [c.identity for c in out] == [{"slug": "x"}, {"slug": "Stripe"}]  # sorted by key
    gh = next(c for c in out if c.family == "greenhouse")
    assert gh.source == SOURCE_CC  # first-seen wins, not the later lever-guess one
    assert identity_key("greenhouse", {"slug": "Stripe"}) == identity_key("greenhouse", {"slug": "stripe"})


def test_http_and_https_copies_of_an_icims_host_are_one_candidate():
    http = candidate_from_url("http://careers-acme.icims.com/jobs/1/job")
    https = candidate_from_url("https://careers-acme.icims.com/jobs/2/job")
    assert http is not None and https is not None
    assert http.identity["base_url"] == "https://careers-acme.icims.com"
    assert [c.identity for c in dedup([http, https])] == [
        {"family": "icims", "slug": "acme", "base_url": "https://careers-acme.icims.com"},
    ]


def test_lever_guesses_carry_the_company_name():
    out = lever_guesses([("Hugging Face", "https://huggingface.co")])
    slugs = {c.identity["slug"] for c in out}
    assert "huggingface" in slugs
    assert all(c.family == "lever" and c.source == SOURCE_LEVER
               and c.company_hint == "Hugging Face" for c in out)


def test_yc_us_companies_filters_status_team_size_and_region():
    cos = [
        YcCompany("Us Co", "us", "Active", 8, "https://us.co", ("United States of America",)),
        YcCompany("Ca Co", "ca", "Active", 8, None, ("America / Canada",)),
        YcCompany("Eu Co", "eu", "Active", 50, None, ("Europe",)),
        YcCompany("Tiny", "tiny", "Active", 2, None, ("United States of America",)),
        YcCompany("Dead", "dead", "Inactive", 50, None, ("United States of America",)),
    ]
    assert yc_us_companies(cos, min_team_size=5) == [("Us Co", "https://us.co"), ("Ca Co", None)]


@pytest.mark.parametrize("url, site", [
    ("https://acme.wd5.myworkdayjobs.com/en-US/Careers/job/1", "Careers"),
    ("https://acme.wd1.myworkdayjobs.com/jobs", "jobs"),
])
def test_workday_sites_named_like_reserved_words_are_kept(url, site):
    c = candidate_from_url(url)
    assert c is not None and c.family == "workday" and c.identity["site"] == site


async def test_supplement_companies_merges_sources_and_swallows_vc_failure(monkeypatch):
    async def a16z(client):
        return [PortfolioCompany("Netris", ["netris"], "netris.io")]

    async def sequoia(client):
        raise RuntimeError("boom")

    monkeypatch.setattr(cand_mod, "a16z_portfolio", a16z)
    monkeypatch.setattr(cand_mod, "sequoia_portfolio", sequoia)
    feed = [
        {"name": "Us Co", "slug": "us", "status": "Active", "team_size": 8,
         "website": "https://us.co", "regions": ["United States of America"]},
        {"name": "Eu Co", "slug": "eu", "status": "Active", "team_size": 50,
         "website": "https://eu.co", "regions": ["Europe"]},
    ]
    with respx.mock:
        respx.get(_FEED_URL).respond(200, json=feed)
        async with httpx.AsyncClient() as client:
            out = await supplement_companies(client)
    assert out == [("Us Co", "https://us.co"), ("Netris", "https://netris.io")]


async def test_supplement_companies_requires_the_yc_feed():
    with respx.mock:
        respx.get(_FEED_URL).respond(500)
        async with httpx.AsyncClient() as client:
            with pytest.raises(httpx.HTTPStatusError):
                await supplement_companies(client)
