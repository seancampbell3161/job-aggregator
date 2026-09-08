def test_headless_is_a_valid_tier():
    from src.handler import _VALID_TIERS
    assert "headless" in _VALID_TIERS


def test_tier_literal_includes_headless():
    import typing
    from src.models import Tier
    assert "headless" in typing.get_args(Tier)
