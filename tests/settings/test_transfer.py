"""Import/export between a directory of files and the settings service."""
import shutil
from pathlib import Path

import pytest
import yaml

from src.settings.errors import NotConfigured, SettingsInvalid
from src.settings.service import ConfigService
from src.settings.sources import append_slug_sources
from src.settings.transfer import (
    ImportFailed, ImportGuardRefused, export_dir, import_dir, unknown_keys,
)
from tests.settings_helpers import make_service

REPO = Path(__file__).resolve().parents[2]


def _examples_dir(tmp_path: Path) -> Path:
    d = tmp_path / "in"
    (d / "resume").mkdir(parents=True)
    shutil.copy(REPO / "config.example.yaml", d / "config.yaml")
    shutil.copy(REPO / "profile.example.md", d / "profile.md")
    shutil.copy(REPO / "resume.md.example", d / "resume.md")
    shutil.copy(REPO / "resume" / "content.example.json", d / "resume" / "content.json")
    shutil.copy(REPO / "resume" / "facts.example.yaml", d / "resume" / "facts.yaml")
    (d / "resume" / "evidence.json").write_text('{"projects": []}')
    return d


def test_import_examples_writes_one_version_and_every_document(tmp_path):
    svc = make_service()
    report = import_dir(svc, _examples_dir(tmp_path), templates_dir=tmp_path / "templates")
    snap = svc.snapshot()
    assert snap.version_id == report.version_id
    assert set(report.documents) == {"profile", "resume_text", "resume_content", "evidence", "kit_facts"}
    assert snap.documents.profile == (REPO / "profile.example.md").read_text()
    assert snap.cfg.sources.greenhouse == ["stripe", "databricks"]
    row = svc.versions()[0]
    assert row.source == "import"
    assert row.note.startswith("import: config.yaml, profile.md")
    assert report.warnings == []


def test_export_then_import_round_trips(tmp_path):
    first = make_service()
    import_dir(first, _examples_dir(tmp_path), templates_dir=tmp_path / "t1")
    export_dir(first, tmp_path / "out", templates_dir=tmp_path / "t1")
    second = make_service()
    import_dir(second, tmp_path / "out", templates_dir=tmp_path / "t2")
    assert second.current_doc()[1] == first.current_doc()[1]
    assert second.documents() == first.documents()


def test_export_is_minimal_and_secret_free(tmp_path):
    svc = make_service({"schedules": {"ats_minutes": 10, "slow_minutes": 30}},
                       secrets={"ntfy_topic_url": "https://ntfy.sh/secret-topic"})
    written = export_dir(svc, tmp_path / "out", templates_dir=tmp_path / "t")
    text = (tmp_path / "out" / "config.yaml").read_text()
    assert yaml.safe_load(text) == {"schedules": {"slow_minutes": 30}}
    assert "secret-topic" not in text
    assert written == ["config.yaml"]


def test_export_copies_user_template_packs(tmp_path):
    packs = tmp_path / "t"
    (packs / "mine").mkdir(parents=True)
    (packs / "mine" / "template.html.j2").write_text("x")
    (packs / ".pending" / "draft").mkdir(parents=True)
    written = export_dir(make_service({}), tmp_path / "out", templates_dir=packs)
    assert "resume/templates/mine/" in written
    assert (tmp_path / "out" / "resume" / "templates" / "mine" / "template.html.j2").exists()
    assert not (tmp_path / "out" / "resume" / "templates" / ".pending").exists()


def test_export_requires_setup(tmp_path):
    with pytest.raises(NotConfigured):
        export_dir(make_service(), tmp_path / "out", templates_dir=tmp_path / "t")


def test_invalid_import_writes_nothing(tmp_path):
    d = _examples_dir(tmp_path)
    (d / "resume" / "content.json").write_text("{broken")
    (d / "resume" / "templates" / "mine").mkdir(parents=True)
    (d / "resume" / "templates" / "mine" / "template.html.j2").write_text("x")
    svc = make_service()
    with pytest.raises(SettingsInvalid) as exc:
        import_dir(svc, d, templates_dir=tmp_path / "t")
    assert [e["loc"] for e in exc.value.errors] == ["resume_content"]
    assert svc.snapshot() is None
    assert svc.documents().profile is None
    assert not (tmp_path / "t").exists()


def test_config_file_is_required(tmp_path):
    with pytest.raises(ImportFailed, match="not found"):
        import_dir(make_service(), tmp_path, templates_dir=tmp_path / "t")


def test_config_must_be_a_mapping(tmp_path):
    (tmp_path / "config.yaml").write_text("- just\n- a list\n")
    with pytest.raises(ImportFailed, match="mapping"):
        import_dir(make_service(), tmp_path, templates_dir=tmp_path / "t")


def test_unknown_keys_are_warned_and_dropped(tmp_path):
    (tmp_path / "config.yaml").write_text(
        "filters:\n  titles: [eng]\n  colour: blue\n"
        "sources:\n  workday:\n  - {tenant: a, region: wd1, site: S, extra: 1}\n"
        "widgets: 3\n"
    )
    svc = make_service()
    report = import_dir(svc, tmp_path, templates_dir=tmp_path / "t")
    assert report.warnings == [
        "filters.colour: unknown key, ignored",
        "sources.workday[0].extra: unknown key, ignored",
        "widgets: unknown key, ignored",
    ]
    assert svc.current_doc()[1] == {
        "filters": {"titles": ["eng"]},
        "sources": {"workday": [{"tenant": "a", "region": "wd1", "site": "S"}]},
    }


def test_legacy_path_keys_pick_the_file_and_are_dropped(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "me.md").write_text("custom profile")
    facts = tmp_path / "abs-facts.yaml"
    facts.write_text("- group: G\n  facts: []\n")
    (tmp_path / "config.yaml").write_text(
        f"relevance:\n  enabled: true\n  profile_path: docs/me.md\nkit:\n  facts_path: {facts}\n"
    )
    (tmp_path / "profile.md").write_text("default-named profile is ignored")
    svc = make_service()
    report = import_dir(svc, tmp_path, templates_dir=tmp_path / "t")
    assert report.warnings == []
    docs = svc.documents()
    assert docs.profile == "custom profile"
    assert docs.kit_facts.startswith("- group: G")
    assert svc.current_doc()[1] == {"relevance": {"enabled": True}}


def test_legacy_path_key_naming_a_missing_file_is_warned_and_skipped(tmp_path):
    (tmp_path / "config.yaml").write_text(
        "relevance:\n  enabled: true\n  profile_path: docs/missing.md\n"
    )
    (tmp_path / "profile.md").write_text("the default-named file is not a fallback")
    svc = make_service()
    report = import_dir(svc, tmp_path, templates_dir=tmp_path / "t")
    assert report.warnings == ["relevance.profile_path: docs/missing.md not found — skipped"]
    assert svc.documents().profile is None
    assert svc.current_doc()[1] == {"relevance": {"enabled": True}}


# -- trust_paths=False (R11): an untrusted caller must not honour a legacy path
# key that reaches outside the extracted archive ----------------------------

def test_trust_paths_defaults_true_so_cli_behaviour_is_unchanged(tmp_path):
    """trust_paths is not passed by the CLI (cli.py's _cmd_import) or by any
    other existing caller -- the default must keep reading an absolute legacy
    path exactly as before."""
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "me.md").write_text("custom profile")
    (tmp_path / "config.yaml").write_text(
        "relevance:\n  enabled: true\n  profile_path: docs/me.md\n"
    )
    svc = make_service()
    report = import_dir(svc, tmp_path, templates_dir=tmp_path / "t")
    assert report.warnings == []
    assert svc.documents().profile == "custom profile"


def test_untrusted_import_ignores_an_absolute_legacy_path(tmp_path):
    outside = tmp_path.parent / "outside-secret.md"
    outside.write_text("SECRET FILE CONTENTS")
    (tmp_path / "config.yaml").write_text(
        f"relevance:\n  enabled: true\n  profile_path: {outside}\n"
    )
    svc = make_service()
    report = import_dir(svc, tmp_path, templates_dir=tmp_path / "t", trust_paths=False)
    assert report.warnings == [
        f"relevance.profile_path: {outside} points outside the archive — ignored"
    ]
    assert svc.documents().profile is None
    assert svc.current_doc()[1] == {"relevance": {"enabled": True}}


def test_untrusted_import_ignores_a_relative_legacy_path_that_escapes_via_dotdot(tmp_path):
    d = tmp_path / "archive"
    d.mkdir()
    outside = tmp_path / "outside-secret.md"
    outside.write_text("SECRET FILE CONTENTS")
    (d / "config.yaml").write_text(
        "relevance:\n  enabled: true\n  profile_path: ../outside-secret.md\n"
    )
    svc = make_service()
    report = import_dir(svc, d, templates_dir=tmp_path / "t", trust_paths=False)
    assert report.warnings == [
        "relevance.profile_path: ../outside-secret.md points outside the archive — ignored"
    ]
    assert svc.documents().profile is None


def test_untrusted_import_still_honours_a_confined_relative_legacy_path(tmp_path):
    """The fix must not break the ordinary, safe case: a relative path that
    stays inside the extracted tree is just a file in the user's own
    archive, and importing it is exactly what a restore is for."""
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "me.md").write_text("custom profile")
    (tmp_path / "config.yaml").write_text(
        "relevance:\n  enabled: true\n  profile_path: docs/me.md\n"
    )
    svc = make_service()
    report = import_dir(svc, tmp_path, templates_dir=tmp_path / "t", trust_paths=False)
    assert report.warnings == []
    assert svc.documents().profile == "custom profile"


def test_untrusted_import_ignores_a_legacy_path_containing_a_nul_byte(tmp_path):
    """PyYAML happily decodes a double-quoted scalar's \\0 escape into a
    string with an embedded NUL byte. Path.resolve() raises ValueError for
    that (unlike .is_file(), which just returns False) -- _confined must
    treat that the same as "not confined" rather than let the ValueError
    escape import_dir uncaught, which would turn an ignorable, warned-about
    upload into an unhandled 500 on the web endpoint."""
    (tmp_path / "config.yaml").write_text(
        'relevance:\n  enabled: true\n  profile_path: "a\\0b"\n'
    )
    svc = make_service()
    report = import_dir(svc, tmp_path, templates_dir=tmp_path / "t", trust_paths=False)
    assert "points outside the archive — ignored" in report.warnings[0]
    assert svc.documents().profile is None
    assert svc.current_doc()[1] == {"relevance": {"enabled": True}}


def test_config_that_is_not_utf8_fails_naming_the_file(tmp_path):
    (tmp_path / "config.yaml").write_bytes(b"schedules: {slow_minutes: 30}\n# caf\xe9\n")
    svc = make_service()
    with pytest.raises(ImportFailed, match=r"config\.yaml: not valid UTF-8"):
        import_dir(svc, tmp_path, templates_dir=tmp_path / "t")
    assert svc.snapshot() is None


def test_document_that_is_not_utf8_fails_naming_the_file_and_writes_nothing(tmp_path):
    (tmp_path / "config.yaml").write_text("{}\n")
    (tmp_path / "resume").mkdir()
    (tmp_path / "resume" / "facts.yaml").write_bytes(b"- group: caf\xe9\n")
    svc = make_service()
    with pytest.raises(ImportFailed, match=r"facts\.yaml: not valid UTF-8"):
        import_dir(svc, tmp_path, templates_dir=tmp_path / "t")
    assert svc.snapshot() is None
    assert svc.generation() == 0


def test_secrets_in_the_config_file_are_ignored_with_a_warning(tmp_path):
    (tmp_path / "config.yaml").write_text("secrets:\n  ntfy_topic_url: https://ntfy.sh/x\n")
    svc = make_service()
    report = import_dir(svc, tmp_path, templates_dir=tmp_path / "t")
    assert any(w.startswith("secrets: ignored") for w in report.warnings)
    assert svc.snapshot().cfg.secrets.ntfy_topic_url == ""


def test_config_path_override(tmp_path):
    (tmp_path / "config.friend.yaml").write_text("schedules: {slow_minutes: 45}\n")
    svc = make_service()
    report = import_dir(svc, tmp_path, config_path=tmp_path / "config.friend.yaml",
                        templates_dir=tmp_path / "t")
    assert svc.snapshot().cfg.schedules.slow_minutes == 45
    assert report.files == ["config.friend.yaml"]


def test_template_packs_are_copied_and_existing_ones_skipped(tmp_path):
    d = tmp_path / "in"
    packs = d / "resume" / "templates"
    for name in ("alpha", "beta", ".pending"):
        (packs / name).mkdir(parents=True)
        (packs / name / "template.html.j2").write_text(name)
    (packs / "not-a-pack").mkdir()
    (d / "config.yaml").write_text("{}\n")
    dest = tmp_path / "dest"
    (dest / "beta").mkdir(parents=True)
    (dest / "beta" / "template.html.j2").write_text("keep")
    report = import_dir(make_service(), d, templates_dir=dest)
    assert report.templates_copied == ["alpha"]
    assert report.templates_skipped == ["beta"]
    assert (dest / "alpha" / "template.html.j2").read_text() == "alpha"
    assert (dest / "beta" / "template.html.j2").read_text() == "keep"


def test_unknown_keys_walks_models_lists_and_unions():
    raw = {
        "filters": {"location": {"planet": "mars"}},
        "sources": {"hiringcafe": {"extra_queries": ["plain", {"query": "q", "loc": "x"}]}},
        "quiet_hours": {"timezone": "UTC", "start": "01:00", "end": "02:00", "tz": 1},
    }
    assert unknown_keys(raw) == [
        "filters.location.planet",
        "sources.hiringcafe.extra_queries[1].loc",
        "quiet_hours.tz",
    ]


# -- re-import guard: settings saved outside an import since the last one -----------

def _config_dir(tmp_path: Path, text: str = "schedules: {slow_minutes: 30}\n") -> Path:
    d = tmp_path / "in"
    d.mkdir(exist_ok=True)
    (d / "config.yaml").write_text(text)
    return d


def test_import_refuses_to_replace_changes_saved_since_the_last_import(tmp_path):
    svc = make_service()
    d = _config_dir(tmp_path)
    import_dir(svc, d, templates_dir=tmp_path / "t")
    append_slug_sources(svc, {"greenhouse": ["stripe"]}, label="add-source")

    def slower(doc):
        doc["schedules"]["slow_minutes"] = 60
        return "slow tier every hour"

    svc.update_settings(slower, source="cli")
    added, tweaked = svc.versions()[1], svc.versions()[0]
    (d / "profile.md").write_text("# edited")
    (d / "resume" / "templates" / "mine").mkdir(parents=True)
    (d / "resume" / "templates" / "mine" / "template.html.j2").write_text("x")
    generation = svc.generation()

    with pytest.raises(ImportGuardRefused) as exc:
        import_dir(svc, d, templates_dir=tmp_path / "t")

    msg = str(exc.value)
    assert f"{tweaked.id}  cli  slow tier every hour" in msg
    assert f"{added.id}  cli  add-source: added greenhouse:stripe" in msg
    assert "after your last import" in msg
    # R13: the shared message stops at the "saved in:" rows -- no
    # surface-specific instruction. "export DIR"/"--force" is the CLI's own
    # closing line (test_cli.py), appended by _print_error, not baked in
    # here where it would be wrong advice for e.g. the web backup page.
    assert "python -m src.settings export DIR" not in msg
    assert "--force" not in msg
    # The refusal wrote nothing: no version, no document, no template pack.
    assert svc.generation() == generation
    assert svc.snapshot().cfg.sources.greenhouse == ["stripe"]
    assert svc.snapshot().cfg.schedules.slow_minutes == 60
    assert svc.documents().profile is None
    assert not (tmp_path / "t").exists()


def test_import_with_force_replaces_changes_saved_since_the_last_import(tmp_path):
    svc = make_service()
    d = _config_dir(tmp_path)
    import_dir(svc, d, templates_dir=tmp_path / "t")
    append_slug_sources(svc, {"greenhouse": ["stripe"]}, label="add-source")
    report = import_dir(svc, d, templates_dir=tmp_path / "t", force=True)
    snap = svc.snapshot()
    assert snap.version_id == report.version_id
    assert snap.cfg.sources.greenhouse == []
    # The forced import is now the last import, so a plain one is allowed again.
    import_dir(svc, d, templates_dir=tmp_path / "t")


def test_import_is_allowed_when_only_imports_came_after_the_last_import(tmp_path):
    svc = make_service()
    d = _config_dir(tmp_path)
    import_dir(svc, d, templates_dir=tmp_path / "t")
    (d / "config.yaml").write_text("schedules: {slow_minutes: 45}\n")
    import_dir(svc, d, templates_dir=tmp_path / "t")
    assert svc.snapshot().cfg.schedules.slow_minutes == 45


def test_import_refuses_when_existing_settings_never_came_from_an_import(tmp_path):
    svc = make_service({"schedules": {"slow_minutes": 20}})  # saved with source=cli
    with pytest.raises(ImportFailed, match="test fixture"):
        import_dir(svc, _config_dir(tmp_path), templates_dir=tmp_path / "t")
    assert svc.snapshot().cfg.schedules.slow_minutes == 20


def test_import_refusal_lists_at_most_ten_versions(tmp_path):
    svc = make_service()
    d = _config_dir(tmp_path)
    import_dir(svc, d, templates_dir=tmp_path / "t")
    for i in range(12):
        svc.save_settings({"schedules": {"slow_minutes": 20 + i}}, source="cli", note=f"change #{i}")
    with pytest.raises(ImportFailed) as exc:
        import_dir(svc, d, templates_dir=tmp_path / "t")
    listed = [line for line in str(exc.value).splitlines() if "change #" in line]
    assert [line.rsplit("#", 1)[1] for line in listed] == [str(i) for i in range(11, 1, -1)]
    assert "and 2 more" in str(exc.value)


@pytest.mark.parametrize("changed_since_the_import", [False, True])
def test_import_does_not_replace_a_save_that_lands_during_the_import(
    tmp_path, monkeypatch, changed_since_the_import,
):
    svc = make_service()
    d = _config_dir(tmp_path)
    import_dir(svc, d, templates_dir=tmp_path / "t")
    if changed_since_the_import:
        # The guard compares the files and lets them through; the write still
        # pins the version it compared against.
        append_slug_sources(svc, {"greenhouse": ["stripe"]}, label="add-source")
        (d / "config.yaml").write_text("schedules: {slow_minutes: 30}\nsources: {greenhouse: [stripe]}\n")
    real_save_bundle = svc.save_bundle

    def save_bundle_after_a_concurrent_write(*args, **kwargs):
        ConfigService(svc._store, env={}).save_settings(
            {"schedules": {"ats_minutes": 3}}, source="cli", note="concurrent")
        return real_save_bundle(*args, **kwargs)

    monkeypatch.setattr(svc, "save_bundle", save_bundle_after_a_concurrent_write)
    with pytest.raises(ImportFailed, match="while importing"):
        import_dir(svc, d, templates_dir=tmp_path / "t")
    assert svc.snapshot().cfg.schedules.ats_minutes == 3


# -- the guard is content-aware: it refuses only what the files would undo ----------------

def _slower(minutes: int):
    def mutate(doc):
        doc.setdefault("schedules", {})["slow_minutes"] = minutes
        return f"slow tier every {minutes} minutes"
    return mutate


def test_export_merge_import_loop_succeeds_without_force(tmp_path):
    svc = make_service()
    d = _config_dir(tmp_path)
    import_dir(svc, d, templates_dir=tmp_path / "t")
    append_slug_sources(svc, {"greenhouse": ["stripe"]}, label="add-source")
    svc.update_settings(_slower(60), source="cli")

    exported = tmp_path / "export"
    export_dir(svc, exported, templates_dir=tmp_path / "t")
    report = import_dir(svc, exported, templates_dir=tmp_path / "t")  # no --force

    cfg = svc.snapshot().cfg
    assert svc.snapshot().version_id == report.version_id
    assert cfg.sources.greenhouse == ["stripe"]
    assert cfg.schedules.slow_minutes == 60


def test_export_merge_import_succeeds_after_a_restore_removed_an_entry(tmp_path):
    svc = make_service()
    d = _config_dir(tmp_path, "sources: {greenhouse: [acme]}\n")
    first = import_dir(svc, d, templates_dir=tmp_path / "t").version_id
    (d / "config.yaml").write_text("sources: {greenhouse: [acme, stripe]}\n")
    import_dir(svc, d, templates_dir=tmp_path / "t")
    svc.restore(first)

    # Merged: stripe stays out, and figma is a new edit.
    (d / "config.yaml").write_text("sources: {greenhouse: [acme, figma]}\n")
    import_dir(svc, d, templates_dir=tmp_path / "t")
    assert svc.snapshot().cfg.sources.greenhouse == ["acme", "figma"]


def test_a_file_edited_after_merging_imports_without_force(tmp_path):
    svc = make_service()
    d = _config_dir(tmp_path)
    import_dir(svc, d, templates_dir=tmp_path / "t")
    append_slug_sources(svc, {"greenhouse": ["stripe"]}, label="add-source")
    (d / "config.yaml").write_text(
        "schedules: {slow_minutes: 30, ats_minutes: 5}\nsources: {greenhouse: [stripe, figma]}\n"
    )
    import_dir(svc, d, templates_dir=tmp_path / "t")
    cfg = svc.snapshot().cfg
    assert cfg.sources.greenhouse == ["stripe", "figma"]
    assert cfg.schedules.ats_minutes == 5


def test_a_new_value_for_a_setting_changed_since_the_import_is_a_deliberate_edit(tmp_path):
    svc = make_service()
    d = _config_dir(tmp_path)
    import_dir(svc, d, templates_dir=tmp_path / "t")
    svc.update_settings(_slower(60), source="cli")
    (d / "config.yaml").write_text("schedules: {slow_minutes: 45}\n")
    import_dir(svc, d, templates_dir=tmp_path / "t")
    assert svc.snapshot().cfg.schedules.slow_minutes == 45


def test_import_refusal_names_each_setting_the_files_would_undo(tmp_path):
    svc = make_service()
    d = _config_dir(tmp_path)
    import_dir(svc, d, templates_dir=tmp_path / "t")
    append_slug_sources(svc, {"greenhouse": ["stripe"]}, label="add-source")
    svc.update_settings(_slower(60), source="cli")
    (d / "config.yaml").write_text("schedules: {slow_minutes: 30}\nsources: {greenhouse: [figma]}\n")

    with pytest.raises(ImportFailed) as exc:
        import_dir(svc, d, templates_dir=tmp_path / "t")

    msg = str(exc.value)
    assert "schedules.slow_minutes: would change 60 back to 30" in msg
    assert "sources.greenhouse: would drop stripe" in msg
    assert svc.snapshot().cfg.sources.greenhouse == ["stripe"]


def test_invalid_files_report_validation_errors_instead_of_the_guard(tmp_path):
    svc = make_service()
    d = _config_dir(tmp_path)
    import_dir(svc, d, templates_dir=tmp_path / "t")
    append_slug_sources(svc, {"greenhouse": ["stripe"]}, label="add-source")
    (d / "config.yaml").write_text("schedules: {ats_minutes: 0}\n")
    with pytest.raises(SettingsInvalid) as exc:
        import_dir(svc, d, templates_dir=tmp_path / "t")
    assert [e["loc"] for e in exc.value.errors] == ["schedules.ats_minutes"]


def test_an_unusable_last_import_falls_back_to_a_strict_comparison(tmp_path):
    svc = make_service()
    d = _config_dir(tmp_path)
    import_dir(svc, d, templates_dir=tmp_path / "t")
    svc._store.insert_settings(doc={"schedules": {"ats_minutes": 0}}, source="import",
                               note="broken import", schema_version=1)
    svc.update_settings(_slower(60), source="cli")

    # Against the last valid import the stale file would look like a deliberate
    # edit; with no usable baseline every value in effect must be kept.
    (d / "config.yaml").write_text("schedules: {slow_minutes: 45}\n")
    with pytest.raises(ImportFailed, match="would change 60 to 45"):
        import_dir(svc, d, templates_dir=tmp_path / "t")

    (d / "config.yaml").write_text("schedules: {slow_minutes: 60, ats_minutes: 5}\n")
    import_dir(svc, d, templates_dir=tmp_path / "t")
    assert svc.snapshot().cfg.schedules.ats_minutes == 5


def test_a_stale_file_cannot_bring_back_default_list_entries_removed_since_the_import(tmp_path):
    svc = make_service()
    d = _config_dir(tmp_path, "schedules: {slow_minutes: 30}\nfilters: {blocked_employment_types: []}\n")
    first = import_dir(svc, d, templates_dir=tmp_path / "t").version_id
    (d / "config.yaml").write_text("schedules: {slow_minutes: 30}\n")
    import_dir(svc, d, templates_dir=tmp_path / "t")
    svc.restore(first)

    with pytest.raises(ImportFailed) as exc:
        import_dir(svc, d, templates_dir=tmp_path / "t")

    assert ("filters.blocked_employment_types: would bring back contract, temporary, "
            "part_time, internship") in str(exc.value)
    assert svc.snapshot().cfg.filters.blocked_employment_types == []


def test_import_refusal_message_ends_at_the_saved_in_rows(tmp_path):
    """R13: ImportGuardRefused's own message is shared, verbatim, content
    only -- head, undone-settings lines, "saved in:" rows -- with no
    surface-specific instruction baked in (that would be wrong advice on a
    surface that isn't the CLI, e.g. the web backup page, which has neither
    a DIR argument nor a --force flag). Each surface appends its own: the
    CLI's in cli.py's _print_error (test_cli.py covers what CLI users see
    end to end), the web backup page's in web/settings/backup.py."""
    svc = make_service()
    d = _config_dir(tmp_path)
    import_dir(svc, d, templates_dir=tmp_path / "t")
    append_slug_sources(svc, {"greenhouse": ["stripe"]}, label="add-source")
    added = svc.versions()[0]
    with pytest.raises(ImportGuardRefused) as exc:
        import_dir(svc, d, templates_dir=tmp_path / "t")
    msg = str(exc.value)
    assert msg.splitlines()[-1] == f"  {added.id}  cli  add-source: added greenhouse:stripe"
    assert "Nothing was written" not in msg
    assert "export DIR" not in msg
    assert "--force" not in msg
