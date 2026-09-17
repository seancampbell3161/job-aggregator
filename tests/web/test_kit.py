# tests/web/test_kit.py
import pytest

from src.kit_facts import FactsError, parse_facts

VALID = """\
- group: Links
  facts:
    - label: GitHub
      value: "https://github.com/example"
    - label: LinkedIn
      value: "https://www.linkedin.com/in/example"
- group: Eligibility
  facts:
    - label: Requires sponsorship
      value: No
    - label: Notice period (weeks)
      value: 2
    - label: Middle name
      value:
"""


def test_parse_facts_parses_groups_in_order():
    groups = parse_facts(VALID)
    assert [g.name for g in groups] == ["Links", "Eligibility"]
    assert [f.label for f in groups[0].facts] == ["GitHub", "LinkedIn"]
    assert groups[0].facts[0].value == "https://github.com/example"


def test_parse_facts_coerces_scalars_to_strings():
    groups = parse_facts(VALID)
    by_label = {f.label: f.value for f in groups[1].facts}
    assert by_label["Requires sponsorship"] == "No"      # unquoted YAML bool
    assert by_label["Notice period (weeks)"] == "2"       # int -> str
    assert by_label["Middle name"] == ""                  # null -> empty string


def test_parse_facts_empty_text_is_no_groups():
    assert parse_facts("") == []


def test_parse_facts_top_level_must_be_list():
    with pytest.raises(FactsError, match="top level"):
        parse_facts("group: Links\n")


def test_parse_facts_entry_missing_group_name():
    with pytest.raises(FactsError, match="entry 1"):
        parse_facts("- facts: []\n")


def test_parse_facts_fact_missing_label():
    with pytest.raises(FactsError, match="Links"):
        parse_facts("- group: Links\n  facts:\n    - value: x\n")


def test_parse_facts_fact_missing_value():
    with pytest.raises(FactsError, match="missing a `value:`"):
        parse_facts("- group: Links\n  facts:\n    - label: GitHub\n")


def test_parse_facts_invalid_yaml_raises_facts_error():
    with pytest.raises(FactsError):
        parse_facts("- group: [unclosed\n")


from tests.auth_helpers import signed_in_client

from src.sqlite_db import connect
from src.web.app import create_app
from src.web.repo import TriageRepo
from tests.settings_helpers import configured_stores


@pytest.fixture
def kit_client(tmp_path, monkeypatch):
    monkeypatch.setenv("JOB_AGG_TAILORED_DIR", str(tmp_path / "tailored"))
    stores = configured_stores(connect(":memory:"))
    app = create_app(repo=TriageRepo(stores.seen), stores=stores)

    def set_facts(text: str) -> None:
        # Raw store insert, no validation: a malformed body stands in for a
        # saved document that a newer parser can no longer read.
        stores.settings.insert_document(kind="kit_facts", body=text, source="cli")

    return signed_in_client(app), set_facts


def test_kit_renders_groups_and_copy_buttons(kit_client):
    client, set_facts = kit_client
    set_facts(VALID)
    r = client.get("/kit")
    assert r.status_code == 200
    assert "Links" in r.text and "Eligibility" in r.text
    assert r.text.index("Links") < r.text.index("Eligibility")  # file order
    assert "https://github.com/example" in r.text
    assert 'data-value="https://github.com/example"' in r.text  # copy payload


def test_kit_missing_file_renders_setup_notice(kit_client):
    client, _ = kit_client
    r = client.get("/kit")
    assert r.status_code == 200
    assert "facts.example.yaml" in r.text  # points at the template file


def test_kit_malformed_file_renders_error_banner(kit_client):
    client, set_facts = kit_client
    set_facts("group: not-a-list\n")
    r = client.get("/kit")
    assert r.status_code == 200
    assert "top level must be a list" in r.text


def test_kit_escapes_values(kit_client):
    client, set_facts = kit_client
    set_facts(
        '- group: X\n  facts:\n    - label: evil\n      value: "<script>alert(1)</script>"\n'
    )
    r = client.get("/kit")
    assert "<script>alert(1)</script>" not in r.text
    assert "&lt;script&gt;" in r.text


def test_nav_links_to_kit(kit_client):
    client, _ = kit_client
    r = client.get("/kit")
    assert 'href="/kit"' in r.text  # base.html nav renders on the page


def test_kit_reflects_a_new_facts_document_without_restart(kit_client):
    client, set_facts = kit_client
    assert "No apply-kit facts yet" in client.get("/kit").text
    set_facts(VALID)
    assert "Eligibility" in client.get("/kit").text


def test_tailor_loading_page_links_to_kit():
    from src.tailor.endpoint.page import loading_page
    html = loading_page("greenhouse:acme:1", "tok", "Engineer", "Acme")
    assert 'href="/kit"' in html


def _groups():
    from src.kit_facts import Fact, FactGroup
    return [FactGroup(name="Links", facts=(Fact("GitHub", "https://github.com/x"),)),
            FactGroup(name="EEO", facts=(Fact("Veteran status", "I am not a protected veteran"),))]


def test_build_bookmarklet_shape():
    import urllib.parse

    from src.web.kit import build_bookmarklet
    bm = build_bookmarklet(_groups(), "function __APPLY_FILL(f){}")
    assert bm.startswith("javascript:")
    body = urllib.parse.unquote(bm[len("javascript:"):])
    assert "function __APPLY_FILL(f){}" in body          # matcher inlined
    assert "__APPLY_FILL(" in body                        # invoked with facts


def test_build_bookmarklet_embeds_facts_roundtrip():
    import json
    import urllib.parse

    from src.web.kit import build_bookmarklet
    bm = build_bookmarklet(_groups(), "M")
    body = urllib.parse.unquote(bm[len("javascript:"):])
    # extract the JSON array passed to __APPLY_FILL(...)
    start = body.index("__APPLY_FILL(") + len("__APPLY_FILL(")
    end = body.rindex(");})()")
    facts = json.loads(body[start:end])
    assert facts[0] == {"group": "Links", "label": "GitHub", "value": "https://github.com/x"}
    assert facts[1]["value"] == "I am not a protected veteran"


def test_build_bookmarklet_escapes_script_and_unicode():
    import json
    import urllib.parse

    from src.kit_facts import Fact, FactGroup
    from src.web.kit import build_bookmarklet
    groups = [FactGroup(name="X", facts=(
        Fact("evil", '</script><b>"\'\\ é 𝟙'),))]
    bm = build_bookmarklet(groups, "M")
    body = urllib.parse.unquote(bm[len("javascript:"):])
    assert "</script>" not in body            # the raw sequence must not appear
    assert "\\u003c/script>" in body          # it is <-escaped instead
    start = body.index("__APPLY_FILL(") + len("__APPLY_FILL(")
    end = body.rindex(");})()")
    facts = json.loads(body[start:end])       # still valid JSON…
    assert facts[0]["value"] == '</script><b>"\'\\ é 𝟙'   # …round-trips exactly


def test_build_bookmarklet_survives_url_parser():
    import urllib.parse

    from src.web.kit import build_bookmarklet
    bm = build_bookmarklet(_groups(), "function __APPLY_FILL(f){/* c */\n  return f;\n}")
    assert bm.startswith("javascript:")
    encoded = bm[len("javascript:"):]
    # WHATWG URL parser strips raw tab/newline — the encoded body must contain none
    assert "\n" not in encoded and "\t" not in encoded and "\r" not in encoded
    # click-time percent-decode restores the body exactly (incl. its newlines)
    body = urllib.parse.unquote(encoded)
    assert body.startswith("(function(){") and body.endswith(");})()")
    assert "__APPLY_FILL(" in body and "\n" in body   # newlines survive via %0A


def test_kit_shows_bookmarklet_when_facts_load(kit_client):
    client, set_facts = kit_client
    set_facts(VALID)
    r = client.get("/kit")
    assert r.status_code == 200
    assert "Apply Autofill" in r.text
    assert 'href="javascript:' in r.text


def test_kit_no_bookmarklet_when_missing(kit_client):
    client, _ = kit_client
    r = client.get("/kit")
    assert 'href="javascript:' not in r.text        # section hidden, no facts


def test_kit_no_bookmarklet_when_malformed(kit_client):
    client, set_facts = kit_client
    set_facts("group: not-a-list\n")
    r = client.get("/kit")
    assert 'href="javascript:' not in r.text
