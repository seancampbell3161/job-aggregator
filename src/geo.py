"""Location-string → country/region resolution.

Single source of truth for geographic data. normalize() resolves posting
location strings into ``country:<iso2>`` / ``region:<name>`` tags;
filter_location() asks whether a posting's resolved area covers any allowed
country (containment: "Remote - Europe" covers DE; "Worldwide" covers all).

Tag values are lowercase ("country:de", "region:eu"). Config-facing codes are
canonical uppercase ISO 3166-1 alpha-2 ("DE") — see validate_country_code().
Promoted from the pre-v0.6 _NON_US_COUNTRIES blocklist in normalize.py, with
the US added as a peer entry.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

# iso2 (lowercase) -> name tokens, matched on word boundaries, case-insensitive.
COUNTRY_NAMES: dict[str, tuple[str, ...]] = {
    "us": ("US", "USA", "United States"),
    # UK & Ireland
    "gb": ("UK", "United Kingdom", "Britain", "England", "Scotland", "Wales",
           "Northern Ireland"),
    "ie": ("Ireland",),
    # EU member states (most common in job postings)
    "at": ("Austria",), "be": ("Belgium",), "bg": ("Bulgaria",),
    "hr": ("Croatia",), "cy": ("Cyprus",), "cz": ("Czechia", "Czech Republic"),
    "dk": ("Denmark",), "ee": ("Estonia",), "fi": ("Finland",),
    "fr": ("France",), "de": ("Germany",), "gr": ("Greece",),
    "hu": ("Hungary",), "it": ("Italy",), "lv": ("Latvia",),
    "lt": ("Lithuania",), "lu": ("Luxembourg",), "mt": ("Malta",),
    "nl": ("Netherlands", "Holland"), "pl": ("Poland",), "pt": ("Portugal",),
    "ro": ("Romania",), "sk": ("Slovakia",), "si": ("Slovenia",),
    "es": ("Spain",), "se": ("Sweden",),
    # Other Europe
    "ch": ("Switzerland",), "no": ("Norway",), "is": ("Iceland",),
    "rs": ("Serbia",), "ua": ("Ukraine",), "tr": ("Turkey",), "ru": ("Russia",),
    # North America (non-US)
    "ca": ("Canada",), "mx": ("Mexico",),
    # Latin America
    "ar": ("Argentina",), "br": ("Brazil", "Brasil"), "cl": ("Chile",),
    "co": ("Colombia",), "pe": ("Peru",), "ve": ("Venezuela",),
    "uy": ("Uruguay",), "cr": ("Costa Rica",), "ec": ("Ecuador",),
    # Asia
    "in": ("India",), "cn": ("China",), "jp": ("Japan",), "sg": ("Singapore",),
    "hk": ("Hong Kong",), "kr": ("South Korea", "Korea"), "vn": ("Vietnam",),
    "ph": ("Philippines",), "id": ("Indonesia",), "th": ("Thailand",),
    "my": ("Malaysia",), "tw": ("Taiwan",), "pk": ("Pakistan",),
    "bd": ("Bangladesh",),
    # Middle East / Africa
    "il": ("Israel",), "ae": ("UAE", "United Arab Emirates"),
    "sa": ("Saudi Arabia",), "eg": ("Egypt",), "za": ("South Africa",),
    "ng": ("Nigeria",), "ke": ("Kenya",), "ma": ("Morocco",),
    # Oceania
    "au": ("Australia",), "nz": ("New Zealand",),
}

# Tokens that need hand-written regexes:
# - us: "U.S." can't take a trailing \b (period then space has no boundary).
# - ie: "Ireland" must not fire inside "Northern Ireland" (that's gb).
_CUSTOM_COUNTRY_PATTERNS: dict[str, re.Pattern[str]] = {
    "us": re.compile(r"\bUS\b|\bUSA\b|U\.S\.|United States", re.I),
    "ie": re.compile(r"\b(?<!Northern )Ireland\b", re.I),
}


def _name_pattern(tokens: tuple[str, ...]) -> re.Pattern[str]:
    return re.compile(
        r"\b(?:" + "|".join(re.escape(t) for t in tokens) + r")\b", re.I
    )


_COUNTRY_PATTERNS: dict[str, re.Pattern[str]] = {
    code: _CUSTOM_COUNTRY_PATTERNS.get(code, _name_pattern(tokens))
    for code, tokens in COUNTRY_NAMES.items()
}

# ISO 3166-1 alpha-2 prefixes used in Ashby/Workday location strings
# ("DE-Berlin", "US-WA-Bellevue"). Matched case-SENSITIVELY at the string
# start so "In-Office - ..." doesn't resolve to India ("IN-").
_PREFIX_TO_COUNTRY: dict[str, str] = {code.upper(): code for code in COUNTRY_NAMES}
_PREFIX_TO_COUNTRY["UK"] = "gb"
_ISO_PREFIX_RE = re.compile(r"^([A-Z]{2})-")

# region key -> lowercase iso2 members; None means "covers every country".
_EU = frozenset({
    "at", "be", "bg", "hr", "cy", "cz", "dk", "ee", "fi", "fr", "de", "gr",
    "hu", "ie", "it", "lv", "lt", "lu", "mt", "nl", "pl", "pt", "ro", "sk",
    "si", "es", "se",
})
_EUROPE = _EU | {"gb", "ch", "no", "is", "rs", "ua"}
_ASIA = frozenset({
    "in", "cn", "jp", "sg", "hk", "kr", "vn", "ph", "id", "th", "my", "tw",
    "pk", "bd",
})
REGION_COUNTRIES: dict[str, frozenset[str] | None] = {
    "worldwide": None,
    "eu": _EU,
    "europe": _EUROPE,
    "emea": _EUROPE | {"tr", "ru", "il", "ae", "sa", "eg", "za", "ng", "ke", "ma"},
    "apac": _ASIA | {"au", "nz"},
    "asia": _ASIA,
    "latam": frozenset({"ar", "br", "cl", "co", "pe", "ve", "uy", "cr", "ec", "mx"}),
    "africa": frozenset({"za", "ng", "ke", "ma", "eg"}),
    "middle_east": frozenset({"il", "ae", "sa", "tr"}),
    "oceania": frozenset({"au", "nz"}),
}

_REGION_TOKENS: dict[str, tuple[str, ...]] = {
    "worldwide": ("Worldwide", "Global", "Anywhere"),
    "eu": ("EU",),
    "europe": ("Europe",),
    "emea": ("EMEA",),
    "apac": ("APAC",),
    "asia": ("Asia",),
    "latam": ("LATAM", "Latin America"),
    "africa": ("Africa",),
    "middle_east": ("Middle East",),
    "oceania": ("Oceania",),
}
_REGION_PATTERNS: dict[str, re.Pattern[str]] = {
    key: _name_pattern(tokens) for key, tokens in _REGION_TOKENS.items()
}

# Config-level aliases accepted by validate_country_code.
COUNTRY_ALIASES: dict[str, str] = {"uk": "gb"}


def resolve_geo_tags(location: str) -> set[str]:
    """Resolve a raw location string to country:/region: tags (may be empty)."""
    found: set[str] = set()
    for code, pat in _COUNTRY_PATTERNS.items():
        if pat.search(location):
            found.add(f"country:{code}")
    for key, pat in _REGION_PATTERNS.items():
        if pat.search(location):
            found.add(f"region:{key}")
    m = _ISO_PREFIX_RE.match(location)
    if m and m.group(1) in _PREFIX_TO_COUNTRY:
        found.add(f"country:{_PREFIX_TO_COUNTRY[m.group(1)]}")
    return found


def geo_tags(tags: Iterable[str]) -> set[str]:
    return {t for t in tags if t.startswith(("country:", "region:"))}


def covers_any(geo: Iterable[str], allowed_countries: Iterable[str]) -> bool:
    """True if the resolved area reaches any allowed country (lowercase iso2).

    ``region:worldwide`` (the generic "Anywhere"/"Worldwide"/"Global" token)
    covers every country, but only when the geo set carries no explicit
    ``country:`` tag: "Remote - Anywhere in the U.S." resolves to
    ``{country:us, region:worldwide}``, and the explicit country wins over the
    generic worldwide token rather than the worldwide token granting coverage
    of unrelated countries. Any other region keeps plain intersection
    semantics regardless of accompanying country tags. Unrecognized regions
    grant no coverage.
    """
    geo = set(geo)
    allowed = set(allowed_countries)
    has_explicit_country = any(tag.startswith("country:") for tag in geo)
    for tag in geo:
        kind, _, value = tag.partition(":")
        if kind == "country" and value in allowed:
            return True
        if kind == "region" and value in REGION_COUNTRIES:
            members = REGION_COUNTRIES[value]
            if members is None:
                if not has_explicit_country:
                    return True
            elif members & allowed:
                return True
    return False


def validate_country_code(raw: str) -> str:
    """Canonicalize a config country code to uppercase iso2; raise on unknown."""
    code = COUNTRY_ALIASES.get(raw.lower(), raw.lower())
    if code not in COUNTRY_NAMES:
        raise ValueError(
            f"unknown country code {raw!r} in location.allowed_countries "
            "(use ISO 3166-1 alpha-2, e.g. US, DE, GB)"
        )
    return code.upper()
