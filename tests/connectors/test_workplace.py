from src.connectors.workplace import annotate_location, remote_from_workplace_type


def test_remote_workplace_type_is_remote():
    assert remote_from_workplace_type("Remote") is True
    assert remote_from_workplace_type("remote") is True


def test_hybrid_and_onsite_are_not_remote():
    # Overrides any separate remote flag (the fallback).
    assert remote_from_workplace_type("Hybrid", fallback=True) is False
    assert remote_from_workplace_type("hybrid") is False
    assert remote_from_workplace_type("OnSite", fallback=True) is False
    assert remote_from_workplace_type("onsite") is False
    assert remote_from_workplace_type("on-site") is False
    assert remote_from_workplace_type("On Site") is False


def test_absent_or_unrecognized_uses_fallback():
    assert remote_from_workplace_type(None) is None
    assert remote_from_workplace_type(None, fallback=True) is True
    assert remote_from_workplace_type("", fallback=False) is False
    # Lever's "unspecified" is not a known type → fall back.
    assert remote_from_workplace_type("unspecified") is None
    assert remote_from_workplace_type("unspecified", fallback=True) is True


def test_annotate_location_folds_in_hybrid_and_onsite():
    assert annotate_location("San Francisco", "Hybrid") == "San Francisco (Hybrid)"
    assert annotate_location("San Francisco", "OnSite") == "San Francisco (Onsite)"
    assert annotate_location("San Francisco", "on-site") == "San Francisco (Onsite)"


def test_annotate_location_leaves_remote_and_absent_untouched():
    # Remote is carried by the remote flag, not annotated here.
    assert annotate_location("San Francisco", "remote") == "San Francisco"
    assert annotate_location("San Francisco", None) == "San Francisco"
    assert annotate_location("San Francisco", "unspecified") == "San Francisco"


def test_annotate_location_no_double_label_and_handles_empty():
    assert annotate_location("Hybrid - NYC", "Hybrid") == "Hybrid - NYC"  # already present
    assert annotate_location(None, "Hybrid") == "Hybrid"  # no base location
