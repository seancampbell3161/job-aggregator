from src.slugging import (
    domain_slug,
    labeled_slug_candidates,
    normalize_name_slug,
    seed_domain,
    slug_candidates,
)
from src.yc_oss import derive_slug


# ---------- normalize_name_slug (variant 1) ----------


def test_normalize_clean_names_match_old_derive_slug():
    # Regression: today's rule is preserved for clean names.
    assert normalize_name_slug("Modal Labs") == "modal-labs"
    assert normalize_name_slug("Stripe") == "stripe"
    assert normalize_name_slug("Period.Co") == "periodco"


def test_normalize_strips_punctuation_and_corporate_suffix():
    # The measured junk case from the spec: old rule produced
    # "diode-computers,-inc" — guaranteed dead on every ATS.
    assert normalize_name_slug("Diode Computers, Inc.") == "diode-computers"
    assert normalize_name_slug("Acme, LLC") == "acme"
    assert normalize_name_slug("Widgets Ltd") == "widgets"
    assert normalize_name_slug("Foo Corp") == "foo"


def test_normalize_collapses_hyphen_runs_from_stripped_ampersand():
    assert normalize_name_slug("Ben & Jerry's") == "ben-jerrys"


def test_normalize_strips_suffix_only_once_and_only_trailing():
    assert normalize_name_slug("Inc Magazine") == "inc-magazine"  # not trailing
    assert normalize_name_slug("LLC Inc") == "llc"                # one strip only


def test_normalize_empty_and_all_punctuation_names():
    assert normalize_name_slug("") == ""
    assert normalize_name_slug("...") == ""


# ---------- domain helpers (variant 2) ----------


def test_domain_slug_strips_scheme_www_and_path():
    assert domain_slug("https://www.airbyte.com/about") == "airbyte"
    assert domain_slug("http://acme.io") == "acme"
    assert domain_slug("getbread.co") == "getbread"  # schemeless yc-oss style


def test_domain_slug_handles_country_registries():
    assert domain_slug("https://airbyte.co.uk") == "airbyte"
    assert domain_slug("https://foo.com.au") == "foo"


def test_domain_slug_bad_input_returns_none():
    assert domain_slug("not a url") is None
    assert domain_slug("") is None


def test_seed_domain_bare_host_for_fingerprint():
    assert seed_domain("https://www.airbyte.com/x") == "airbyte.com"
    assert seed_domain("acme.io/careers") == "acme.io"
    assert seed_domain("nonsense") is None


# ---------- ordered, deduped chain ----------


def test_labeled_candidates_full_chain_order_and_kinds():
    got = labeled_slug_candidates("Diode Computers, Inc.", "https://diode.dev", "diode-computers-yc")
    assert got == [
        ("slug", "diode-computers"),
        ("domain", "diode"),
        ("alt", "diode-computers-yc"),
    ]


def test_candidates_dedupe_and_cap():
    # name-slug == domain-slug == alt → one variant.
    assert slug_candidates("Airbyte", "https://airbyte.com", "airbyte") == ["airbyte"]


def test_candidates_empty_name_falls_to_domain_then_alt():
    assert slug_candidates("", "https://airbyte.com", "yc-alt") == ["airbyte", "yc-alt"]
    assert slug_candidates("", None, "yc-alt") == ["yc-alt"]
    assert slug_candidates("", None, None) == []


def test_derive_slug_is_wrapper_over_variant_1():
    assert derive_slug("Diode Computers, Inc.") == normalize_name_slug("Diode Computers, Inc.")
