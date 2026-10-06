import pytest

from scripts.pack_builder.locations import classify


@pytest.mark.parametrize("loc, expected", [
    ("San Francisco, CA", {"us"}),
    ("New York, NY", {"us"}),
    ("Boston, MA 02110", {"us"}),
    ("Remote - US", {"us"}),
    ("Austin, Texas, United States", {"us"}),
    ("Denver, Colorado", {"us"}),
    ("Berlin, Germany", {"eu"}),
    ("London, UK", {"eu"}),
    ("Remote - Europe", {"eu"}),
    ("Berlin, DE", set()),             # DE is Delaware or Germany: not counted as US
    ("Pune, IN", set()),               # IN is Indiana or India: not counted as US
    ("Toronto, ON, Canada", set()),
    ("Remote", set()),
    ("", set()),
    (None, set()),
    ("New York, NY or London, UK", {"us", "eu"}),
    # Foreign tech hubs with state-abbreviation collisions (CA/IL/CO/WA)
    ("Toronto, CA", set()),
    ("Calgary, Alberta, CA", set()),
    ("Vancouver, BC, CA", set()),
    ("Montréal, QC, CA", set()),
    ("Tel Aviv, IL", set()),
    ("Herzliya, IL", set()),
    ("Bogota, CO", set()),
    ("Medellín, CO", set()),
    ("Perth, WA", set()),
    ("Tel Aviv, Israel", set()),
])
def test_classify(loc, expected):
    assert classify(loc) == frozenset(expected)
