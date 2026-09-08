import json
from pathlib import Path

import httpx
import pytest
import respx

from src.yc_oss import (
    YcCompany,
    YcOssClient,
    derive_slug,
    derive_slug_for,
    filter_companies,
    parse_companies,
)

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "yc_oss_sample.json").read_text()
)


def test_parse_companies_returns_typed_records():
    companies = parse_companies(FIXTURE)
    assert len(companies) == 8
    stripe = next(c for c in companies if c.name == "Stripe")
    assert stripe.status == "Public"
    assert stripe.team_size == 8000


def test_parse_companies_treats_missing_team_size_as_zero():
    companies = parse_companies(FIXTURE)
    missing = next(c for c in companies if c.name == "MissingTeamSize")
    assert missing.team_size == 0


def test_parse_companies_returns_empty_on_unrecognized_shape():
    assert parse_companies({"unexpected": "shape"}) == []
    assert parse_companies(None) == []  # type: ignore[arg-type]


def test_filter_companies_keeps_active_and_public():
    companies = parse_companies(FIXTURE)
    kept = filter_companies(companies, min_team_size=10)
    names = {c.name for c in kept}
    assert "Stripe" in names           # Public, team_size 8000
    assert "Modal Labs" in names       # Active, team_size 35
    assert "Acme Co" in names          # Active, team_size 12
    assert "Period.Co" in names        # Active, team_size 25


def test_filter_companies_drops_inactive_and_acquired():
    companies = parse_companies(FIXTURE)
    kept = filter_companies(companies, min_team_size=10)
    names = {c.name for c in kept}
    assert "Bygone Labs" not in names
    assert "Eaten Up" not in names


def test_filter_companies_drops_below_team_size_threshold():
    companies = parse_companies(FIXTURE)
    kept = filter_companies(companies, min_team_size=10)
    names = {c.name for c in kept}
    assert "Tinyship" not in names         # team_size 3
    assert "MissingTeamSize" not in names  # team_size 0


def test_derive_slug_lowercases_and_hyphenates_spaces():
    assert derive_slug("Modal Labs") == "modal-labs"
    assert derive_slug("Stripe") == "stripe"


def test_derive_slug_strips_dots():
    assert derive_slug("Period.Co") == "periodco"


def test_derive_slug_for_falls_back_to_yc_slug_when_name_empty():
    c = YcCompany(name="", slug="fallback", status="Active", team_size=10, website=None)
    assert derive_slug_for(c) == "fallback"


def test_derive_slug_for_returns_empty_when_both_name_and_slug_missing():
    c = YcCompany(name="", slug="", status="Active", team_size=10, website=None)
    assert derive_slug_for(c) == ""


@pytest.mark.asyncio
async def test_client_fetch_returns_filtered_companies():
    """End-to-end: client hits yc-oss, parses, filters."""
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get("https://yc-oss.github.io/api/companies/all.json").respond(
                200, json=FIXTURE
            )
            cli = YcOssClient(min_team_size=10)
            kept = await cli.fetch(client)

    names = {c.name for c in kept}
    assert "Stripe" in names
    assert "Tinyship" not in names


@pytest.mark.asyncio
async def test_client_returns_empty_on_404():
    """If yc-oss is down, the client logs and returns []."""
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get("https://yc-oss.github.io/api/companies/all.json").respond(404)
            cli = YcOssClient(min_team_size=10)
            kept = await cli.fetch(client)

    assert kept == []


@pytest.mark.asyncio
async def test_client_returns_empty_on_unrecognized_payload():
    """If yc-oss returns a non-list payload, the client returns []."""
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get("https://yc-oss.github.io/api/companies/all.json").respond(
                200, json={"unexpected": "shape"}
            )
            cli = YcOssClient(min_team_size=10)
            kept = await cli.fetch(client)

    assert kept == []
