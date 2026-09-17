import pytest

from src.settings.errors import NotConfigured
from src.settings.sources import append_slug_sources
from tests.settings_helpers import make_service


def test_append_slug_sources_adds_and_dedupes():
    svc = make_service({"sources": {"greenhouse": ["stripe"]}})
    added = append_slug_sources(svc, {"greenhouse": ["stripe", "figma"], "lever": ["acme"]}, label="test")
    assert added == {"greenhouse": ["figma"], "lever": ["acme"]}
    cfg = svc.snapshot().cfg
    assert cfg.sources.greenhouse == ["stripe", "figma"]
    assert cfg.sources.lever == ["acme"]
    assert svc.versions()[0].note == "test: added greenhouse:figma, lever:acme"


def test_append_slug_sources_is_idempotent():
    svc = make_service({"sources": {"greenhouse": ["stripe"]}})
    before = len(svc.versions())
    assert append_slug_sources(svc, {"greenhouse": ["stripe"]}, label="test") == {}
    assert len(svc.versions()) == before


def test_append_slug_sources_rejects_structured_families():
    with pytest.raises(ValueError, match="unknown slug source family"):
        append_slug_sources(make_service({}), {"workday": ["x"]}, label="test")


def test_append_slug_sources_summarizes_large_batches():
    svc = make_service({})
    append_slug_sources(svc, {"greenhouse": [f"co{i}" for i in range(8)]}, label="seed_companies")
    assert svc.versions()[0].note == "seed_companies: added 8 slugs"


def test_append_slug_sources_requires_setup():
    with pytest.raises(NotConfigured):
        append_slug_sources(make_service(), {"lever": ["acme"]}, label="test")
