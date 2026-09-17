"""The warnings Overview shows. Each names a way this instance silently does
nothing useful."""
from src.config import AppConfig
from src.web.settings.readiness import check


def _check(doc=None, *, has_profile=True, secrets=()):
    cfg = AppConfig.model_validate(doc or {})
    return {w.code for w in check(cfg, has_profile=has_profile,
                                  secret_source=lambda n: "stored" if n in secrets else "unset")}


def test_empty_titles_is_reported():
    assert "no_titles" in _check()


def test_titles_set_clears_it():
    assert "no_titles" not in _check({"filters": {"titles": ["platform engineer"]}})


def test_nothing_polled_when_no_sources_and_discovery_off():
    # hn_who_is_hiring/remotive/remoteok default to enabled=True (free, no
    # config needed), so a bare {} doc already has pollable sources — this
    # has to turn them off explicitly to represent "nothing configured".
    doc = {"sources": {"hn_who_is_hiring": {"enabled": False},
                       "remotive": {"enabled": False},
                       "remoteok": {"enabled": False}}}
    assert "nothing_polled" in _check(doc)


def test_one_slug_source_clears_nothing_polled():
    assert "nothing_polled" not in _check({"sources": {"greenhouse": ["stripe"]}})


def test_an_enabled_aggregator_clears_nothing_polled():
    assert "nothing_polled" not in _check({"sources": {"remotive": {"enabled": True}}})


def test_discovery_enabled_clears_nothing_polled():
    assert "nothing_polled" not in _check({"discovery": {"enabled": True}})


def test_no_delivery_sink_is_reported():
    assert "no_sink" in _check()
    assert "no_sink" not in _check(secrets=("ntfy_topic_url",))
    assert "no_sink" not in _check(secrets=("discord_webhook_url",))


def test_llm_enabled_without_a_key_is_reported():
    doc = {"relevance": {"enabled": True, "provider": "anthropic"}}
    assert "llm_no_key" in _check(doc)
    assert "llm_no_key" not in _check(doc, secrets=("anthropic_api_key",))


def test_local_ollama_needs_no_key():
    doc = {"relevance": {"enabled": True, "provider": "ollama",
                         "ollama_host": "http://ollama:11434"}}
    assert "llm_no_key" not in _check(doc)


def test_hosted_ollama_does_need_a_key():
    doc = {"relevance": {"enabled": True, "provider": "ollama",
                         "ollama_host": "https://ollama.com"}}
    assert "llm_no_key" in _check(doc)


def test_llm_enabled_without_a_profile_is_reported():
    doc = {"relevance": {"enabled": True}}
    assert "llm_no_profile" in _check(doc, has_profile=False, secrets=("anthropic_api_key",))
    assert "llm_no_profile" not in _check(doc, has_profile=True, secrets=("anthropic_api_key",))


def test_unset_max_age_is_reported():
    assert "no_max_age" in _check()
    assert "no_max_age" not in _check({"filters": {"max_age_days": 2}})


def test_every_warning_points_at_a_real_section():
    from src.web.settings.sections import section_by_slug
    cfg = AppConfig.model_validate({"relevance": {"enabled": True}})
    for w in check(cfg, has_profile=False, secret_source=lambda n: "unset"):
        assert section_by_slug(w.fix_slug) is not None, w.code
