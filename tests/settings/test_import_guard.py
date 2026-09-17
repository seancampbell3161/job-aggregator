"""undone_changes: does importing a file undo settings saved since the last import?"""
from src.config import AppConfig
from src.settings.import_guard import undone_changes


def cfg(doc: dict | None = None) -> AppConfig:
    return AppConfig.model_validate(doc or {})


QUIET = {"timezone": "America/New_York", "start": "22:00", "end": "07:00"}
QUIET_SHOWN = '{"end": "07:00:00", "start": "22:00:00", "timezone": "America/New_York"}'


# -- with a baseline (the last import's settings) ---------------------------------------

def test_nothing_changed_since_the_import_allows_any_file():
    base = cfg({"schedules": {"slow_minutes": 30}})
    assert undone_changes(base, base, cfg({"schedules": {"slow_minutes": 90}})) == []


def test_a_stale_scalar_is_reported():
    base = cfg({"schedules": {"slow_minutes": 30}})
    current = cfg({"schedules": {"slow_minutes": 60}})
    assert undone_changes(base, current, base) == [
        "schedules.slow_minutes: would change 60 back to 30"
    ]


def test_a_stale_scalar_that_was_a_default_is_reported():
    current = cfg({"schedules": {"slow_minutes": 60}})
    assert undone_changes(cfg(), current, cfg()) == [
        "schedules.slow_minutes: would change 60 back to 15 (the default)"
    ]


def test_a_scalar_reset_to_its_default_since_the_import_is_reported():
    base = cfg({"schedules": {"slow_minutes": 30}})
    assert undone_changes(base, cfg(), base) == [
        "schedules.slow_minutes: would change 15 (the default) back to 30"
    ]


def test_a_new_value_for_a_changed_scalar_is_a_deliberate_edit():
    base = cfg({"schedules": {"slow_minutes": 30}})
    current = cfg({"schedules": {"slow_minutes": 60}})
    assert undone_changes(base, current, cfg({"schedules": {"slow_minutes": 45}})) == []


def test_a_merged_file_is_allowed():
    base = cfg({"schedules": {"slow_minutes": 30}})
    current = cfg({"schedules": {"slow_minutes": 60}, "sources": {"greenhouse": ["stripe"]}})
    assert undone_changes(base, current, current) == []


def test_list_entries_added_since_the_import_must_be_in_the_file():
    base = cfg({"sources": {"greenhouse": ["acme"]}})
    current = cfg({"sources": {"greenhouse": ["acme", "stripe", "figma"]}})
    incoming = cfg({"sources": {"greenhouse": ["acme", "figma", "linear"]}})
    assert undone_changes(base, current, incoming) == [
        "sources.greenhouse: would drop stripe"
    ]


def test_a_list_family_created_since_the_import_counts():
    current = cfg({"sources": {"lever": ["acme"]}})
    assert undone_changes(cfg(), current, cfg()) == ["sources.lever: would drop acme"]


def test_list_entries_removed_since_the_import_must_not_come_back():
    base = cfg({"sources": {"greenhouse": ["acme", "stripe"]}})
    current = cfg({"sources": {"greenhouse": ["acme"]}})
    assert undone_changes(base, current, base) == [
        "sources.greenhouse: would bring back stripe"
    ]


def test_a_reordered_list_is_not_a_change():
    base = cfg({"sources": {"greenhouse": ["acme", "stripe"]}})
    current = cfg({"sources": {"greenhouse": ["stripe", "acme"]}})
    assert undone_changes(base, current, base) == []


def test_structured_list_entries_compare_by_value():
    current = cfg({"sources": {"workday": [{"tenant": "acme", "region": "wd5", "site": "External"}]}})
    assert undone_changes(cfg(), current, cfg()) == [
        'sources.workday: would drop {"region": "wd5", "site": "External", "tenant": "acme"}'
    ]
    same_entry = cfg({"sources": {"workday": [{"site": "External", "region": "wd5", "tenant": "acme"}]}})
    assert undone_changes(cfg(), current, same_entry) == []


def test_mixed_plain_and_structured_entries_compare_by_value():
    base = cfg({"sources": {"hiringcafe": {"extra_queries": ["rust"]}}})
    current = cfg({"sources": {"hiringcafe": {"extra_queries": ["rust", {"query": "go", "location": "de"}]}}})
    assert undone_changes(base, current, base) == [
        'sources.hiringcafe.extra_queries: would drop {"location": "DE", "query": "go"}'
    ]


def test_long_lists_of_lost_entries_are_summarized():
    current = cfg({"sources": {"greenhouse": [f"co{i}" for i in range(8)]}})
    assert undone_changes(cfg(), current, cfg()) == [
        "sources.greenhouse: would drop co0, co1, co2, co3, co4 and 3 more"
    ]


def test_settings_only_in_the_file_are_new_edits():
    base = cfg({"schedules": {"slow_minutes": 30}})
    assert undone_changes(base, base, cfg({"filters": {"titles": ["engineer"]}})) == []


def test_every_undone_setting_is_reported_in_settings_order():
    base = cfg({"filters": {"comp_floor_usd": 150000}})
    current = cfg({"schedules": {"slow_minutes": 60, "ats_minutes": 3},
                   "sources": {"greenhouse": ["stripe"]}})
    assert undone_changes(base, current, base) == [
        "filters.comp_floor_usd: would change 0 (the default) back to 150000",
        "sources.greenhouse: would drop stripe",
        "schedules.ats_minutes: would change 3 back to 10 (the default)",
        "schedules.slow_minutes: would change 60 back to 15 (the default)",
    ]


# -- lists whose defaults are not empty ---------------------------------------------------

def test_a_default_list_emptied_since_the_import_must_stay_empty():
    current = cfg({"filters": {"blocked_employment_types": []}})
    assert undone_changes(cfg(), current, cfg()) == [
        "filters.blocked_employment_types: would bring back contract, temporary, part_time, internship"
    ]


def test_entries_added_to_a_default_list_are_named_alone():
    current = cfg({"filters": {"location": {"allowed_countries": ["US", "CA"]}}})
    assert undone_changes(cfg(), current, cfg()) == [
        "filters.location.allowed_countries: would drop CA"
    ]


def test_a_merged_file_may_drop_a_default_entry_on_purpose():
    current = cfg({"filters": {"seniority_allow": ["mid", "senior", "staff"]}})
    incoming = cfg({"filters": {"seniority_allow": ["senior", "staff"]}})
    assert undone_changes(cfg(), current, incoming) == []


def test_default_entries_restored_since_the_import_must_be_in_the_file():
    base = cfg({"filters": {"seniority_allow": ["mid"]}})
    assert undone_changes(base, cfg(), cfg({"filters": {"seniority_allow": ["junior"]}})) == [
        "filters.seniority_allow: would drop senior"
    ]
    merged = cfg({"filters": {"seniority_allow": ["mid", "senior", "staff"]}})
    assert undone_changes(base, cfg(), merged) == []


# -- optional settings --------------------------------------------------------------------

def test_an_optional_section_removed_since_the_import_must_stay_removed():
    base = cfg({"quiet_hours": QUIET})
    assert undone_changes(base, cfg(), base) == [f"quiet_hours: would set it back to {QUIET_SHOWN}"]


def test_an_optional_section_removed_since_the_import_may_come_back_changed():
    base = cfg({"quiet_hours": QUIET})
    changed = cfg({"quiet_hours": {**QUIET, "timezone": "Europe/Berlin"}})
    assert undone_changes(base, cfg(), changed) == []


def test_an_optional_section_added_since_the_import_must_stay():
    current = cfg({"quiet_hours": QUIET})
    assert undone_changes(cfg(), current, cfg()) == [
        f"quiet_hours: would unset it (now {QUIET_SHOWN})"
    ]


def test_an_optional_section_that_stays_set_compares_field_by_field():
    base = cfg({"quiet_hours": QUIET})
    current = cfg({"quiet_hours": {**QUIET, "timezone": "Europe/Berlin"}})
    assert undone_changes(base, current, base) == [
        "quiet_hours.timezone: would change Europe/Berlin back to America/New_York"
    ]


def test_an_optional_value_set_since_the_import_must_stay_set():
    current = cfg({"filters": {"max_age_days": 2}})
    assert undone_changes(cfg(), current, cfg()) == ["filters.max_age_days: would unset it (now 2)"]


def test_strings_that_would_read_ambiguously_are_quoted():
    base = cfg({"http": {"user_agent": "bot/1.0"}})
    current = cfg({"http": {"user_agent": ""}})
    assert undone_changes(base, current, base) == [
        'http.user_agent: would change "" back to bot/1.0'
    ]
    titles = cfg({"filters": {"titles": ["Engineer, Platform", " staff "]}})
    assert undone_changes(cfg(), titles, cfg()) == [
        'filters.titles: would drop "Engineer, Platform", " staff "'
    ]


# -- without a baseline (settings were never imported) -----------------------------------

def test_without_a_baseline_every_value_in_effect_must_match():
    current = cfg({"schedules": {"slow_minutes": 20}, "sources": {"greenhouse": ["stripe"]}})
    assert undone_changes(None, current, cfg({"schedules": {"slow_minutes": 30}})) == [
        "sources.greenhouse: would drop stripe",
        "schedules.slow_minutes: would change 20 to 30",
    ]


def test_without_a_baseline_a_file_that_keeps_every_value_is_allowed():
    current = cfg({"schedules": {"slow_minutes": 20}, "sources": {"greenhouse": ["stripe"]}})
    incoming = cfg({"schedules": {"slow_minutes": 20, "ats_minutes": 5},
                    "sources": {"greenhouse": ["stripe", "figma"]}})
    assert undone_changes(None, current, incoming) == []


def test_without_a_baseline_dropping_a_value_to_its_default_is_reported():
    assert undone_changes(None, cfg({"schedules": {"slow_minutes": 20}}), cfg()) == [
        "schedules.slow_minutes: would change 20 to 15 (the default)"
    ]


def test_without_a_baseline_settings_at_their_defaults_may_change():
    incoming = cfg({"schedules": {"slow_minutes": 45}, "filters": {"seniority_allow": ["staff"]}})
    assert undone_changes(None, cfg(), incoming) == []


def test_without_a_baseline_a_default_list_reports_only_what_changed():
    added = cfg({"filters": {"seniority_allow": ["mid", "senior", "staff"]}})
    assert undone_changes(None, added, cfg()) == ["filters.seniority_allow: would drop staff"]
    removed = cfg({"filters": {"seniority_allow": ["mid"]}})
    assert undone_changes(None, removed, cfg()) == ["filters.seniority_allow: would bring back senior"]


def test_without_a_baseline_an_optional_section_in_effect_must_be_kept():
    current = cfg({"quiet_hours": QUIET})
    assert undone_changes(None, current, cfg({"quiet_hours": {**QUIET, "start": "23:00"}})) == [
        "quiet_hours.start: would change 22:00:00 to 23:00:00"
    ]
    assert undone_changes(None, current, cfg()) == [f"quiet_hours: would unset it (now {QUIET_SHOWN})"]
