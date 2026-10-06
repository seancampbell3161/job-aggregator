from src.cadence import MAX_INTERVAL_S, next_interval


def test_new_board_quiet_doubles_from_base():
    assert next_interval(None, fresh=0, forced=False, base_s=600) == 1200


def test_quiet_doubles_and_caps():
    assert next_interval(1200, fresh=0, forced=False, base_s=600) == 2400
    assert next_interval(2400, fresh=0, forced=False, base_s=600) == MAX_INTERVAL_S
    assert next_interval(MAX_INTERVAL_S, fresh=0, forced=False, base_s=600) == MAX_INTERVAL_S


def test_new_postings_reset_to_base():
    assert next_interval(MAX_INTERVAL_S, fresh=3, forced=False, base_s=600) == 600


def test_forced_resets_to_base():
    assert next_interval(MAX_INTERVAL_S, fresh=0, forced=True, base_s=600) == 600
