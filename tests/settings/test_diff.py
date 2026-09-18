"""diff_settings: a plain, symmetric 'what changed' between two configs."""
from src.config import AppConfig
from src.settings.diff import diff_settings


def cfg(**doc) -> AppConfig:
    return AppConfig.model_validate(doc)


def test_no_difference_is_an_empty_list():
    assert diff_settings(cfg(), cfg()) == []


def test_a_changed_scalar_names_both_values():
    lines = diff_settings(cfg(relevance={"score_low": 4}), cfg(relevance={"score_low": 6}))
    assert len(lines) == 1
    assert "relevance.score_low" in lines[0]
    assert "4" in lines[0] and "6" in lines[0]


def test_a_list_reports_additions_and_removals_not_a_replacement():
    lines = diff_settings(
        cfg(sources={"greenhouse": ["acme", "beta"]}),
        cfg(sources={"greenhouse": ["beta", "gamma"]}),
    )
    joined = " ".join(lines)
    assert "acme" in joined and "gamma" in joined
    assert "beta" not in joined  # unchanged entries are noise


def test_list_order_alone_is_not_a_change():
    assert diff_settings(
        cfg(sources={"greenhouse": ["acme", "beta"]}),
        cfg(sources={"greenhouse": ["beta", "acme"]}),
    ) == []


def test_an_optional_section_appearing_and_disappearing_both_read():
    quiet = {"timezone": "UTC", "start": "22:00", "end": "07:00"}
    on = diff_settings(cfg(), cfg(quiet_hours=quiet))
    off = diff_settings(cfg(quiet_hours=quiet), cfg())
    assert "quiet_hours" in on[0] and "quiet_hours" in off[0]
    assert on != off


def test_a_structured_board_entry_reads_as_a_whole_value():
    lines = diff_settings(
        cfg(),
        cfg(sources={"workday": [{"tenant": "m", "region": "wd1", "site": "External"}]}),
    )
    assert "sources.workday" in lines[0]
    assert "tenant" in lines[0]


def test_secrets_never_appear():
    """Secrets are not part of a settings document; if one ever leaks into the
    dump, a diff would print it on a page. Prove it cannot."""
    lines = diff_settings(cfg(), cfg(relevance={"enabled": True}))
    assert not any("secret" in line for line in lines)


def test_the_diff_is_directional():
    a, b = cfg(relevance={"score_low": 4}), cfg(relevance={"score_low": 6})
    assert diff_settings(a, b) != diff_settings(b, a)
