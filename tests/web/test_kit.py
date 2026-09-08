# tests/web/test_kit.py
import pytest

from src.web.kit import FactsError, load_facts

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


def _write(tmp_path, text):
    p = tmp_path / "facts.yaml"
    p.write_text(text)
    return str(p)


def test_load_facts_parses_groups_in_order(tmp_path):
    groups = load_facts(_write(tmp_path, VALID))
    assert [g.name for g in groups] == ["Links", "Eligibility"]
    assert [f.label for f in groups[0].facts] == ["GitHub", "LinkedIn"]
    assert groups[0].facts[0].value == "https://github.com/example"


def test_load_facts_coerces_scalars_to_strings(tmp_path):
    groups = load_facts(_write(tmp_path, VALID))
    by_label = {f.label: f.value for f in groups[1].facts}
    assert by_label["Requires sponsorship"] == "No"      # unquoted YAML bool
    assert by_label["Notice period (weeks)"] == "2"       # int -> str
    assert by_label["Middle name"] == ""                  # null -> empty string


def test_load_facts_missing_file_raises_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_facts(str(tmp_path / "nope.yaml"))


def test_load_facts_top_level_must_be_list(tmp_path):
    with pytest.raises(FactsError, match="top level"):
        load_facts(_write(tmp_path, "group: Links\n"))


def test_load_facts_entry_missing_group_name(tmp_path):
    with pytest.raises(FactsError, match="entry 1"):
        load_facts(_write(tmp_path, "- facts: []\n"))


def test_load_facts_fact_missing_label(tmp_path):
    bad = "- group: Links\n  facts:\n    - value: x\n"
    with pytest.raises(FactsError, match="Links"):
        load_facts(_write(tmp_path, bad))


def test_load_facts_fact_missing_value(tmp_path):
    bad = "- group: Links\n  facts:\n    - label: GitHub\n"
    with pytest.raises(FactsError, match="missing a `value:`"):
        load_facts(_write(tmp_path, bad))


def test_load_facts_invalid_yaml_raises_facts_error(tmp_path):
    with pytest.raises(FactsError):
        load_facts(_write(tmp_path, "- group: [unclosed\n"))


from fastapi.testclient import TestClient

from src.sqlite_db import connect
from src.state_sqlite import (
    SqliteConnectorHealthStore,
    SqliteDiscoveredSlugsStore,
    SqliteOpsAlertStateStore,
    SqlitePipelineEventsStore,
    SqliteRejectedPostingsStore,
    SqliteSeenJobsStore,
    SqliteSourceStateStore,
)
from src.stores import Stores
from src.web.app import create_app
from src.web.repo import TriageRepo


@pytest.fixture
def kit_client(tmp_path, monkeypatch):
    monkeypatch.setenv("JOB_AGG_TAILORED_DIR", str(tmp_path / "tailored"))
    conn = connect(":memory:")
    seen = SqliteSeenJobsStore(conn)
    stores = Stores(
        seen=seen, source_state=SqliteSourceStateStore(conn),
        discovered=SqliteDiscoveredSlugsStore(conn),
        health=SqliteConnectorHealthStore(conn),
        events=SqlitePipelineEventsStore(conn),
        rejected=SqliteRejectedPostingsStore(conn),
        alert_state=SqliteOpsAlertStateStore(conn),
    )
    facts_path = tmp_path / "facts.yaml"
    app = create_app(repo=TriageRepo(seen), stores=stores,
                     kit_facts_path=str(facts_path))
    return TestClient(app), facts_path


def test_kit_renders_groups_and_copy_buttons(kit_client):
    client, facts_path = kit_client
    facts_path.write_text(VALID)
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
    client, facts_path = kit_client
    facts_path.write_text("group: not-a-list\n")
    r = client.get("/kit")
    assert r.status_code == 200
    assert "top level must be a list" in r.text


def test_kit_escapes_values(kit_client):
    client, facts_path = kit_client
    facts_path.write_text(
        '- group: X\n  facts:\n    - label: evil\n      value: "<script>alert(1)</script>"\n'
    )
    r = client.get("/kit")
    assert "<script>alert(1)</script>" not in r.text
    assert "&lt;script&gt;" in r.text


def test_nav_links_to_kit(kit_client):
    client, _ = kit_client
    r = client.get("/kit")
    assert 'href="/kit"' in r.text  # base.html nav renders on the page


def test_tailor_loading_page_links_to_kit():
    from src.tailor.endpoint.page import loading_page
    html = loading_page("greenhouse:acme:1", "tok", "Engineer", "Acme")
    assert 'href="/kit"' in html


def _groups():
    from src.web.kit import Fact, FactGroup
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

    from src.web.kit import Fact, FactGroup, build_bookmarklet
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
    client, facts_path = kit_client
    facts_path.write_text(VALID)
    r = client.get("/kit")
    assert r.status_code == 200
    assert "Apply Autofill" in r.text
    assert 'href="javascript:' in r.text


def test_kit_no_bookmarklet_when_missing(kit_client):
    client, _ = kit_client
    r = client.get("/kit")
    assert 'href="javascript:' not in r.text        # section hidden, no facts


def test_kit_no_bookmarklet_when_malformed(kit_client):
    client, facts_path = kit_client
    facts_path.write_text("group: not-a-list\n")
    r = client.get("/kit")
    assert 'href="javascript:' not in r.text
