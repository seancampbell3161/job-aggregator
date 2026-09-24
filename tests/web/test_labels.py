from src.settings.documents import DOCUMENT_KINDS
from src.web.labels import choice_label, document_label


def test_underscores_become_spaces_and_first_letter_capitalised():
    assert choice_label("contract_to_hire") == "Contract to hire"
    assert choice_label("senior") == "Senior"


def test_override_for_an_abbreviation():
    assert choice_label("ic") == "Individual contributor"


def test_empty_is_empty():
    assert choice_label("") == ""


def test_every_document_kind_has_a_plain_name():
    for kind in DOCUMENT_KINDS:
        assert "_" not in document_label(kind), kind
    assert document_label("kit_facts") == "Apply kit facts"
