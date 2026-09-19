"""The apply-kit scaffold: deterministic, and structurally incapable of
answering an EEO question."""
import yaml

from src.config import AppConfig, FiltersConfig, LocationFilterConfig
from src.kit_facts import parse_facts
from src.resume_intake.facts_scaffold import build_facts_yaml

CONTACT = {
    "email": "s@example.com", "phone": "+1 555 0100",
    "github": "https://github.com/sc", "linkedin": "https://linkedin.com/in/sc",
}


def _cfg(**filters):
    return AppConfig(filters=FiltersConfig(**filters))


def _values(text):
    return {f"{g.name}/{f.label}": f.value for g in parse_facts(text) for f in g.facts}


def test_the_scaffold_parses_with_the_real_parser():
    groups = parse_facts(build_facts_yaml(CONTACT, _cfg()))
    assert [g.name for g in groups] == ["Links", "Eligibility", "EEO"]


def test_links_come_from_the_drafted_contact_block():
    values = _values(build_facts_yaml(CONTACT, _cfg()))
    assert values["Links/GitHub"] == "https://github.com/sc"
    assert values["Links/Email"] == "s@example.com"


def test_location_and_salary_come_from_the_settings_document():
    """NOT from the wizard's answers blob: those fields bind to real config
    paths, and this page is reachable by someone who never ran the wizard."""
    cfg = _cfg(location=LocationFilterConfig(allowed_cities=["Chicago, IL"]),
               comp_floor_usd=180000)
    values = _values(build_facts_yaml(CONTACT, cfg))
    assert values["Eligibility/Location"] == "Chicago, IL"
    assert values["Eligibility/Desired salary (USD)"] == "180000"


def test_location_falls_back_to_countries_when_no_city_is_set():
    """allowed_countries cannot be empty — its validator rejects that
    (src/config.py:45-50) — so the country list is always a usable fallback."""
    cfg = _cfg(location=LocationFilterConfig(allowed_countries=["US", "GB"]))
    assert _values(build_facts_yaml(CONTACT, cfg))["Eligibility/Location"] == "US, GB"


def test_eeo_values_are_always_blank():
    """The load-bearing assertion. An LLM must never answer these, and this
    function has no path by which model output could reach them."""
    hostile = {**CONTACT, "gender": "male", "veteran": "yes",
               "email": "Gender: male. Veteran status: yes."}
    groups = {g.name: g for g in parse_facts(build_facts_yaml(hostile, _cfg()))}
    eeo = groups["EEO"]
    assert [f.label for f in eeo.facts] == ["Gender", "Race/ethnicity",
                                            "Veteran status", "Disability"]
    assert all(f.value == "" for f in eeo.facts)


def test_eligibility_answers_the_llm_cannot_know_are_blank():
    values = _values(build_facts_yaml(CONTACT, _cfg()))
    assert values["Eligibility/Work authorization"] == ""
    assert values["Eligibility/Requires sponsorship"] == ""


def test_the_scaffold_quotes_a_value_yaml_would_otherwise_reinterpret():
    """An unquoted `No` parses as the boolean False — the trap
    resume/facts.example.yaml warns about. Asserting through parse_facts
    cannot catch it: kit_facts._coerce maps False back to the string "No",
    so the round trip looks correct even when the quoting is gone. Parse the
    raw YAML instead, where the bool is still a bool."""
    text = build_facts_yaml({**CONTACT, "website": "No"}, _cfg())
    raw = yaml.safe_load(text)
    site = next(f["value"] for g in raw for f in g["facts"]
                if f["label"] == "Personal site")
    assert site == "No"
    assert isinstance(site, str)
