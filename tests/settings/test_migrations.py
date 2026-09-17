import pytest

from src.settings.migrations import MIGRATIONS, SCHEMA_VERSION, migrate


def test_shipped_migration_list_matches_schema_version():
    assert len(MIGRATIONS) == SCHEMA_VERSION - 1


def test_current_version_is_identity():
    assert migrate({"a": 1}, SCHEMA_VERSION) == {"a": 1}


def test_applies_steps_in_order_starting_at_the_rows_version():
    calls = []

    def v1_to_v2(d):
        calls.append("1->2")
        return {**d, "b": d["a"] + 1}

    def v2_to_v3(d):
        calls.append("2->3")
        return {**d, "c": d["b"] * 10}

    steps = [v1_to_v2, v2_to_v3]
    assert migrate({"a": 1}, 1, migrations=steps, to_version=3) == {"a": 1, "b": 2, "c": 20}
    assert calls == ["1->2", "2->3"]
    calls.clear()
    assert migrate({"a": 0, "b": 5}, 2, migrations=steps, to_version=3) == {"a": 0, "b": 5, "c": 50}
    assert calls == ["2->3"]


@pytest.mark.parametrize("version", [0, 4])
def test_out_of_range_version_raises(version):
    with pytest.raises(ValueError, match="not supported"):
        migrate({}, version, migrations=[lambda d: d, lambda d: d], to_version=3)


def test_mismatched_migration_list_raises():
    with pytest.raises(ValueError, match="expected 2 migrations"):
        migrate({}, 1, migrations=[lambda d: d], to_version=3)
