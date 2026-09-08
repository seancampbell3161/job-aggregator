import pytest


@pytest.mark.integration
def test_smoke_marker_present():
    """Sentinel integration test so the weekly job has at least one item to run.
    Replace/extend as live-endpoint coverage is added."""
    assert True
