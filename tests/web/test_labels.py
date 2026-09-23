from src.web.labels import choice_label


def test_underscores_become_spaces_and_first_letter_capitalised():
    assert choice_label("contract_to_hire") == "Contract to hire"
    assert choice_label("senior") == "Senior"


def test_override_for_an_abbreviation():
    assert choice_label("ic") == "Individual contributor"


def test_empty_is_empty():
    assert choice_label("") == ""
