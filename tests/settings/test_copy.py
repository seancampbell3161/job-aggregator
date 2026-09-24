import re

from src.settings.copy import COPY, field_copy, secret_label
from src.settings.fields import field_map
from src.web.settings.sections import SECTIONS

_HAND_BUILT = [s for s in SECTIONS if s.paths and s.slug != "companies"]
_JARGON = re.compile(r"`|\bnull\b|\b[a-z0-9]+(?:_[a-z0-9]+)+\b")


def _curated_paths() -> set[str]:
    paths = {p for s in _HAND_BUILT for p in s.paths}
    paths |= {f"secrets.{n}" for s in SECTIONS for n in s.secrets}
    return paths


def test_every_hand_built_field_and_secret_has_copy():
    missing = _curated_paths() - COPY.keys()
    assert not missing, sorted(missing)


def test_copy_has_no_stale_entries():
    stale = COPY.keys() - _curated_paths()
    assert not stale, sorted(stale)


def test_copy_is_free_of_config_jargon():
    for path, c in COPY.items():
        for text in (c.label, c.hint, c.blank or ""):
            assert not _JARGON.search(text), f"{path}: {text!r}"


def test_every_optional_curated_field_says_what_blank_means():
    fields = field_map()
    for s in _HAND_BUILT:
        for p in s.paths:
            if fields[p].optional:
                assert COPY[p].blank, p


def test_field_copy_misses_return_none():
    assert field_copy("discovery.enabled") is None


def test_secret_label_prefers_copy_and_falls_back():
    assert secret_label("anthropic_api_key") == "Anthropic API key"
    assert secret_label("tailor_signing_secret") == "tailor signing secret"
