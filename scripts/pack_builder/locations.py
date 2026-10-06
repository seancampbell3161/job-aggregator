"""Classify a posting location as US and/or EU, for ranking the pack.

Builder-local on purpose. The app's location gate leaves "San Francisco, CA"
unresolved (Verdict.UNKNOWN; US cities match through allowed_cities), and that
must not change. Here we only need a per-board tally, so US state names and
abbreviations count as US unless the string names another country."""
from __future__ import annotations

import re

from src.geo import REGION_COUNTRIES, resolve_geo_tags

_STATE_NAMES = (
    "Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado", "Connecticut",
    "Delaware", "Florida", "Georgia", "Hawaii", "Idaho", "Illinois", "Indiana", "Iowa",
    "Kansas", "Kentucky", "Louisiana", "Maine", "Maryland", "Massachusetts", "Michigan",
    "Minnesota", "Mississippi", "Missouri", "Montana", "Nebraska", "Nevada",
    "New Hampshire", "New Jersey", "New Mexico", "New York", "North Carolina",
    "North Dakota", "Ohio", "Oklahoma", "Oregon", "Pennsylvania", "Rhode Island",
    "South Carolina", "South Dakota", "Tennessee", "Texas", "Utah", "Vermont", "Virginia",
    "Washington", "West Virginia", "Wisconsin", "Wyoming", "District of Columbia",
)
# DE and IN are left out: "Berlin, DE" and "Pune, IN" are far more common in
# job feeds than Delaware/Indiana written as abbreviations.
_STATE_ABBRS = (
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "FL", "GA", "HI", "ID", "IL", "IA", "KS",
    "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT",
    "VT", "VA", "WA", "WV", "WI", "WY", "DC",
)
_STATE_RE = re.compile(
    r",\s*(?:" + "|".join(_STATE_ABBRS) + r")\b"
    r"|\b(?:" + "|".join(re.escape(n) for n in _STATE_NAMES) + r")\b"
)
# Foreign tech hubs whose feeds write "City, XX" with XX colliding with a US
# state code (CA = Canada, IL = Israel, CO = Colombia, WA = Western Australia).
# A hub name vetoes the state-abbreviation match; extend as new leaks show up.
_FOREIGN_HUBS = re.compile(
    r"\b(?:Toronto|Vancouver|Montr[eé]al|Ottawa|Calgary|Edmonton|Waterloo|Kitchener"
    r"|Winnipeg|Halifax|Qu[eé]bec|Mississauga|Tel Aviv|Jerusalem|Haifa|Herzliya"
    r"|Petah Tikva|Ra'?anana|Bogot[aá]|Medell[ií]n|Perth)\b",
    re.I,
)
_EUROPE = REGION_COUNTRIES["europe"]
_EUROPE_REGIONS = frozenset({"region:europe", "region:eu"})


def classify(location: str | None) -> frozenset[str]:
    if not location:
        return frozenset()
    tags = resolve_geo_tags(location)
    countries = {t.split(":", 1)[1] for t in tags if t.startswith("country:")}
    out: set[str] = set()
    # A state match counts unless the string names a non-US, non-European
    # country ("Vancouver, BC, Canada"). European countries don't veto it, so
    # a multi-site "New York, NY or London, UK" counts for both regions.
    state_us = (_STATE_RE.search(location) and not countries - {"us"} - _EUROPE
                and not _FOREIGN_HUBS.search(location))
    if "us" in countries or state_us:
        out.add("us")
    if countries & _EUROPE or tags & _EUROPE_REGIONS:
        out.add("eu")
    return frozenset(out)
