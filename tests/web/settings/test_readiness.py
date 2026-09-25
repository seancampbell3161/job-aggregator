"""The warnings Overview shows. Each names a way this instance silently does
nothing useful."""
from src.config import AppConfig
from src.web.settings.readiness import check


def _check(doc=None, *, has_profile=True, secrets=()):
    cfg = AppConfig.model_validate(doc or {})
    return {w.code for w in check(cfg, has_profile=has_profile,
                                  secret_source=lambda n: "stored" if n in secrets else "unset")}


# hn_who_is_hiring/remotive/remoteok default to enabled=True (free, no config
# needed), so any "X clears nothing_polled" test has to turn them off
# explicitly first — otherwise it would still pass with its own signal
# deleted, since the three free aggregators alone already clear the warning.
_NO_AGGREGATORS = {"hn_who_is_hiring": {"enabled": False},
                   "remotive": {"enabled": False},
                   "remoteok": {"enabled": False}}


def test_empty_titles_is_reported():
    assert "no_titles" in _check()


def test_titles_set_clears_it():
    assert "no_titles" not in _check({"filters": {"titles": ["platform engineer"]}})


def test_nothing_polled_when_no_sources_and_discovery_off():
    # A bare {} doc already has pollable sources (see _NO_AGGREGATORS above),
    # so this has to turn them off explicitly to represent "nothing configured".
    assert "nothing_polled" in _check({"sources": _NO_AGGREGATORS})


def test_one_slug_source_clears_nothing_polled():
    doc = {"sources": {**_NO_AGGREGATORS, "greenhouse": ["stripe"]}}
    assert "nothing_polled" not in _check(doc)


def test_an_enabled_aggregator_clears_nothing_polled():
    doc = {"sources": {**_NO_AGGREGATORS, "remotive": {"enabled": True}}}
    assert "nothing_polled" not in _check(doc)


def test_discovery_enabled_clears_nothing_polled():
    doc = {"sources": _NO_AGGREGATORS, "discovery": {"enabled": True}}
    assert "nothing_polled" not in _check(doc)


def _pack(monkeypatch, n):
    from src.starter_pack import PackSlug, StarterPack
    pack = StarterPack("t", tuple(PackSlug("lever", f"c{i}", None, "us", 1) for i in range(n)), ())
    monkeypatch.setattr("src.starter_pack.default_pack", lambda: pack)


def test_starter_pack_clears_nothing_polled(monkeypatch):
    _pack(monkeypatch, 3)
    doc = {"sources": _NO_AGGREGATORS, "discovery": {"starter_pack": True}}
    assert "nothing_polled" not in _check(doc)


def test_empty_starter_pack_does_not_clear_nothing_polled(monkeypatch):
    """An empty (or missing/corrupt) pack polls nothing, so the flag alone is
    not a source."""
    _pack(monkeypatch, 0)
    doc = {"sources": _NO_AGGREGATORS, "discovery": {"starter_pack": True, "enabled": False}}
    assert "nothing_polled" in _check(doc)


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


def test_a_provider_missing_from_the_key_map_does_not_crash(monkeypatch):
    """Adding a provider to relevance.provider's Literal in src/config.py
    without a matching entry in readiness._PROVIDER_KEYS must not 500
    /settings/overview — the page a first-time user's POST /setup/start
    sends them to. Simulated by removing a known-good entry rather than by
    constructing an out-of-Literal AppConfig (Pydantic would reject that)."""
    from src.web.settings import readiness

    monkeypatch.delitem(readiness._PROVIDER_KEYS, "anthropic")
    doc = {"relevance": {"enabled": True, "provider": "anthropic"}}
    codes = _check(doc)  # must not raise
    assert "llm_no_key" not in codes  # can't check a key it doesn't know the name of


def test_every_warning_points_at_a_real_section():
    from src.web.settings.sections import section_by_slug
    cfg = AppConfig.model_validate({"relevance": {"enabled": True}})
    for w in check(cfg, has_profile=False, secret_source=lambda n: "unset"):
        assert section_by_slug(w.fix_slug) is not None, w.code
