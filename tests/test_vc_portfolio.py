from __future__ import annotations

from src.vc_portfolio import (
    PortfolioCompany, name_slugs, _slug_hit_to_result, _bare_domain,
)
from src.fingerprint import connector_name, FingerprintResult


def test_name_slugs_simple():
    assert name_slugs("Figma") == ["figma"]


def test_name_slugs_two_words_yields_both_variants():
    assert name_slugs("Hugging Face") == ["huggingface", "hugging-face"]


def test_name_slugs_strips_corporate_suffixes():
    # "Inc"/"Technologies" are stripped before slug derivation
    assert name_slugs("Acme Technologies Inc") == ["acme"]


def test_name_slugs_all_tokens_stripped_falls_back_to_squash():
    assert name_slugs("[untitled]") == ["untitled"]


def test_bare_domain_extracts_host():
    assert _bare_domain("https://www.netris.io/careers") == "netris.io"
    assert _bare_domain("netris.io") == "netris.io"


def test_bare_domain_none_for_empty_or_bad():
    assert _bare_domain("") is None
    assert _bare_domain("not-a-domain") is None  # no dot


def test_slug_hit_normalizes_to_matched_fingerprint_result():
    c = PortfolioCompany(name="Figma", slug_candidates=["figma"], domain="figma.com")
    r = _slug_hit_to_result(c, "greenhouse", "figma", 42)
    assert r.status == "matched"
    assert r.family == "greenhouse"
    assert r.identity == {"slug": "figma"}
    assert r.posting_count == 42
    assert connector_name(r) == "greenhouse:figma"


def test_slug_hit_tolerates_missing_domain():
    c = PortfolioCompany(name="X", slug_candidates=["x"], domain=None)
    r = _slug_hit_to_result(c, "lever", "x", 1)
    assert r.domain == ""  # FingerprintResult.domain is a str


import httpx
import pytest
import respx

from src.vc_portfolio import a16z_in_profile, a16z_portfolio


def test_a16z_in_profile_keeps_enterprise():
    assert a16z_in_profile({"status": "Active", "verticals": "Enterprise"}) is True


def test_a16z_in_profile_keeps_compound_in_profile_label():
    assert a16z_in_profile({"status": "Active", "verticals": "American Dynamism;Enterprise"}) is True


def test_a16z_in_profile_excludes_pure_crypto():
    assert a16z_in_profile({"status": "Active", "verticals": "Crypto"}) is False


def test_a16z_in_profile_excludes_exited():
    assert a16z_in_profile({"status": "Exited", "verticals": "Enterprise"}) is False


def test_a16z_in_profile_handles_missing_verticals():
    assert a16z_in_profile({"status": "Active"}) is False


_A16Z_PAGE = (
    '<html><div x-data="wr25Portfolio()" data-companies="'
    # HTML-entity-escaped JSON (as the real page embeds it)
    '[{&quot;name&quot;:&quot;Netris&quot;,&quot;status&quot;:&quot;Active&quot;,'
    '&quot;verticals&quot;:&quot;Infra&quot;,&quot;company_url&quot;:&quot;https://netris.io&quot;,'
    '&quot;permalink&quot;:&quot;https://a16z.com/companies/netris/&quot;},'
    '{&quot;name&quot;:&quot;CryptoCo&quot;,&quot;status&quot;:&quot;Active&quot;,'
    '&quot;verticals&quot;:&quot;Crypto&quot;,&quot;company_url&quot;:&quot;https://cryptoco.com&quot;,'
    '&quot;permalink&quot;:&quot;https://a16z.com/companies/cryptoco/&quot;},'
    '{&quot;name&quot;:&quot;[untitled]&quot;,&quot;status&quot;:&quot;Active&quot;,'
    '&quot;verticals&quot;:&quot;Enterprise&quot;,&quot;company_url&quot;:&quot;&quot;,'
    '&quot;permalink&quot;:&quot;&quot;}]'
    '"></div></html>'
)


@respx.mock
@pytest.mark.asyncio
async def test_a16z_portfolio_filters_and_extracts_domain_and_slug():
    respx.get("https://a16z.com/portfolio/").mock(return_value=httpx.Response(200, text=_A16Z_PAGE))
    async with httpx.AsyncClient() as client:
        cos = await a16z_portfolio(client)
    # CryptoCo filtered out (pure crypto); [untitled] skipped
    assert [c.name for c in cos] == ["Netris"]
    netris = cos[0]
    assert netris.domain == "netris.io"
    # curated permalink slug first, then name-derived
    assert netris.slug_candidates[0] == "netris"


@respx.mock
@pytest.mark.asyncio
async def test_a16z_portfolio_raises_on_page_structure_change():
    respx.get("https://a16z.com/portfolio/").mock(return_value=httpx.Response(200, text="<html>nope</html>"))
    with pytest.raises(RuntimeError):
        async with httpx.AsyncClient() as client:
            await a16z_portfolio(client)


_A16Z_NON_LIST_PAGE = (
    '<html><div x-data="wr25Portfolio()" data-companies="'
    '{&quot;name&quot;:&quot;X&quot;}'
    '"></div></html>'
)


@respx.mock
@pytest.mark.asyncio
async def test_a16z_portfolio_raises_on_non_list_json():
    respx.get("https://a16z.com/portfolio/").mock(
        return_value=httpx.Response(200, text=_A16Z_NON_LIST_PAGE)
    )
    with pytest.raises(RuntimeError):
        async with httpx.AsyncClient() as client:
            await a16z_portfolio(client)


# Dedicated fixture (kept separate from _A16Z_PAGE) where the permalink slug
# differs from the name-derived slugs, so ordering/dedup can actually be
# distinguished from the "they happen to match" case above.
_A16Z_PERMALINK_PAGE = (
    '<html><div x-data="wr25Portfolio()" data-companies="'
    '[{&quot;name&quot;:&quot;Hugging Face&quot;,&quot;status&quot;:&quot;Active&quot;,'
    '&quot;verticals&quot;:&quot;Infra&quot;,&quot;company_url&quot;:&quot;https://huggingface.co&quot;,'
    '&quot;permalink&quot;:&quot;https://a16z.com/companies/hugging-face-hq/&quot;}]'
    '"></div></html>'
)


@respx.mock
@pytest.mark.asyncio
async def test_a16z_slug_ordering_permalink_first():
    respx.get("https://a16z.com/portfolio/").mock(return_value=httpx.Response(200, text=_A16Z_PERMALINK_PAGE))
    async with httpx.AsyncClient() as client:
        cos = await a16z_portfolio(client)
    assert len(cos) == 1
    hf = cos[0]
    assert hf.slug_candidates == ["hugging-face-hq", "huggingface", "hugging-face"]
    assert hf.domain == "huggingface.co"


from src.vc_portfolio import sequoia_portfolio


def _wp_company(name, slug):
    return {"title": {"rendered": name}, "slug": slug}


@respx.mock
@pytest.mark.asyncio
async def test_sequoia_paginates_via_totalpages_header():
    route = respx.get("https://sequoiacap.com/wp-json/wp/v2/company")
    route.side_effect = [
        httpx.Response(200, headers={"X-WP-TotalPages": "2"},
                       json=[_wp_company("SendCutSend", "sendcutsend")]),
        httpx.Response(200, headers={"X-WP-TotalPages": "2"},
                       json=[_wp_company("Parallel Web Systems", "parallel-web-systems")]),
    ]
    async with httpx.AsyncClient() as client:
        cos = await sequoia_portfolio(client)
    assert {c.name for c in cos} == {"SendCutSend", "Parallel Web Systems"}
    scs = next(c for c in cos if c.name == "SendCutSend")
    assert scs.slug_candidates[0] == "sendcutsend"   # wp slug first
    assert scs.domain is None                          # sequoia API carries no domain


@respx.mock
@pytest.mark.asyncio
async def test_sequoia_unescapes_html_entities_in_title():
    respx.get("https://sequoiacap.com/wp-json/wp/v2/company").mock(
        return_value=httpx.Response(200, headers={"X-WP-TotalPages": "1"},
                                    json=[_wp_company("Ben &amp; Co", "ben-co")]))
    async with httpx.AsyncClient() as client:
        cos = await sequoia_portfolio(client)
    assert cos[0].name == "Ben & Co"


@respx.mock
@pytest.mark.asyncio
async def test_sequoia_stops_on_empty_page():
    respx.get("https://sequoiacap.com/wp-json/wp/v2/company").mock(
        return_value=httpx.Response(200, headers={"X-WP-TotalPages": "99"}, json=[]))
    async with httpx.AsyncClient() as client:
        cos = await sequoia_portfolio(client)
    assert cos == []


@respx.mock
@pytest.mark.asyncio
async def test_sequoia_raises_on_first_page_error():
    respx.get("https://sequoiacap.com/wp-json/wp/v2/company").mock(
        return_value=httpx.Response(403))
    with pytest.raises(RuntimeError):
        async with httpx.AsyncClient() as client:
            await sequoia_portfolio(client)


@respx.mock
@pytest.mark.asyncio
async def test_sequoia_returns_partial_on_later_page_error():
    route = respx.get("https://sequoiacap.com/wp-json/wp/v2/company")
    route.side_effect = [
        httpx.Response(200, headers={"X-WP-TotalPages": "3"},
                       json=[_wp_company("SendCutSend", "sendcutsend")]),
        httpx.Response(500),
    ]
    async with httpx.AsyncClient() as client:
        cos = await sequoia_portfolio(client)
    assert [c.name for c in cos] == ["SendCutSend"]


from pathlib import Path

from src.vc_portfolio import csv_portfolio


def test_csv_reads_name_and_optional_domain(tmp_path):
    p = tmp_path / "firms.csv"
    p.write_text("name,domain\nFigma,figma.com\nOpenAI\n")
    cos = csv_portfolio(p)
    assert [c.name for c in cos] == ["Figma", "OpenAI"]
    assert cos[0].domain == "figma.com"
    assert cos[0].slug_candidates == ["figma"]
    assert cos[1].domain is None   # no domain column on that row


def test_csv_without_header(tmp_path):
    p = tmp_path / "firms.csv"
    p.write_text("Anduril,anduril.com\n")
    cos = csv_portfolio(p)
    assert cos[0].name == "Anduril"
    assert cos[0].domain == "anduril.com"


def test_csv_skips_blank_lines(tmp_path):
    p = tmp_path / "firms.csv"
    p.write_text("Figma\n\n\nStripe\n")
    cos = csv_portfolio(p)
    assert [c.name for c in cos] == ["Figma", "Stripe"]


import src.vc_portfolio as vc


@respx.mock
@pytest.mark.asyncio
async def test_discover_two_stage_slug_then_fingerprint_residual(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("sources: {}\n")
    # Driver returns 2 companies: one the slug-probe will claim, one it won't
    # (but which has a domain → goes to the fingerprint residual).
    companies = [
        PortfolioCompany(name="Figma", slug_candidates=["figma"], domain="figma.com"),
        PortfolioCompany(name="Pave", slug_candidates=["pave"], domain="pave.com"),
    ]

    async def fake_driver(client):
        return companies
    monkeypatch.setattr(vc, "a16z_portfolio", fake_driver)

    # Stage 1: figma hits greenhouse; pave misses every startup ATS.
    async def fake_probe(*, client, ats_family, slug):
        if slug == "figma" and ats_family == "greenhouse":
            return True, 7
        return False, 0
    monkeypatch.setattr(vc, "_probe_one_ats", fake_probe)

    # Stage 2: fingerprint only pave (the residual-with-domain) → matched.
    async def fake_fp(client, seed):
        assert seed.name == "Pave"   # figma was already claimed; not fingerprinted
        return FingerprintResult(name="Pave", domain="pave.com", status="matched",
                                 family="greenhouse", identity={"slug": "paveaka"}, posting_count=3)
    monkeypatch.setattr(vc, "fingerprint_company", fake_fp)

    async with httpx.AsyncClient() as client:
        results = await vc.discover_portfolio("a16z", client=client, config_path=cfg)

    by_name = {r.name: r for r in results}
    assert connector_name(by_name["Figma"]) == "greenhouse:figma"     # slug-normalized
    assert connector_name(by_name["Pave"]) == "greenhouse:paveaka"    # fingerprint
    assert len(results) == 2


@respx.mock
@pytest.mark.asyncio
async def test_discover_skips_fingerprint_when_no_domain(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("sources: {}\n")
    companies = [PortfolioCompany(name="X", slug_candidates=["x"], domain=None)]

    async def fake_driver(client):
        return companies
    monkeypatch.setattr(vc, "sequoia_portfolio", fake_driver)

    async def fake_probe(*, client, ats_family, slug):
        return False, 0   # slug-probe misses
    monkeypatch.setattr(vc, "_probe_one_ats", fake_probe)

    async def fake_fp(client, seed):
        raise AssertionError("fingerprint must not run for a domain-less company")
    monkeypatch.setattr(vc, "fingerprint_company", fake_fp)

    async with httpx.AsyncClient() as client:
        results = await vc.discover_portfolio("sequoia", client=client, config_path=cfg)
    assert results == []


@respx.mock
@pytest.mark.asyncio
async def test_discover_prefilters_manual_companies(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("discovery:\n  manual_companies: [figma]\nsources: {}\n")
    companies = [PortfolioCompany(name="Figma", slug_candidates=["figma"], domain="figma.com")]

    async def fake_driver(client):
        return companies
    monkeypatch.setattr(vc, "a16z_portfolio", fake_driver)

    async def fake_probe(*, client, ats_family, slug):
        raise AssertionError("prefiltered company must not be probed")
    monkeypatch.setattr(vc, "_probe_one_ats", fake_probe)

    async with httpx.AsyncClient() as client:
        results = await vc.discover_portfolio("a16z", client=client, config_path=cfg)
    assert results == []


@respx.mock
@pytest.mark.asyncio
async def test_discover_dedups_same_identity(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("sources: {}\n")
    # Two companies whose slug-probe resolves to the SAME board identity.
    companies = [
        PortfolioCompany(name="A", slug_candidates=["dup"], domain=None),
        PortfolioCompany(name="B", slug_candidates=["dup"], domain=None),
    ]

    async def fake_driver(client):
        return companies
    monkeypatch.setattr(vc, "a16z_portfolio", fake_driver)

    async def fake_probe(*, client, ats_family, slug):
        return (ats_family == "greenhouse" and slug == "dup"), 1
    monkeypatch.setattr(vc, "_probe_one_ats", fake_probe)

    async with httpx.AsyncClient() as client:
        results = await vc.discover_portfolio("a16z", client=client, config_path=cfg)
    assert [connector_name(r) for r in results] == ["greenhouse:dup"]  # deduped to one
