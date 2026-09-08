"""End-to-end guard for docs/examples/eu-config.md: the yaml block in the doc
is loaded verbatim, so the worked example can't drift from the code."""
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from src.config import FiltersConfig, LocationFilterConfig
from src.filters import Verdict, filter_location
from src.models import RawPosting
from src.normalize import normalize

DOC = Path("docs/examples/eu-config.md")


def _example_location() -> LocationFilterConfig:
    m = re.search(r"```yaml\n(.*?)```", DOC.read_text(), re.S)
    assert m, "eu-config.md must contain a yaml block"
    data = yaml.safe_load(m.group(1))
    return LocationFilterConfig(**data["location"])


def _filters() -> FiltersConfig:
    return FiltersConfig(
        titles=["software engineer"],
        seniority_allow=["mid", "senior"],
        location=_example_location(),
        comp_floor_usd=0,
        stack_any_of=["python"],
    )


def _normalized(location: str):
    raw = RawPosting(
        source="greenhouse:acme",
        external_id="1",
        title="Senior Software Engineer",
        description="",
        apply_url="https://x",
        location=location,
        remote=None,
        posted_at=datetime(2026, 7, 10, tzinfo=timezone.utc),
    )
    return normalize(raw, stack_keywords=["python"], allowed_cities=_example_location().allowed_cities)


def test_example_yaml_parses_to_de_config():
    loc = _example_location()
    assert loc.allowed_countries == ["DE"]
    assert loc.remote_policy == "allowed_countries"


@pytest.mark.parametrize("location", [
    "Remote - Europe",
    "Remote - EMEA",
    "Remote - Germany",
    "Remote, Worldwide",
    "Berlin, Germany",
    "Munich (Hybrid)",
])
def test_eu_config_matches(location):
    assert filter_location(_normalized(location), _filters()) is Verdict.MATCH


@pytest.mark.parametrize("location", [
    "Remote - US only",
    "Remote - UK",
    "Remote - APAC",
    "Paris, France",
    "London, UK",
    "Remote - Anywhere in the U.S.",
])
def test_eu_config_rejects(location):
    assert filter_location(_normalized(location), _filters()) is Verdict.REJECT
