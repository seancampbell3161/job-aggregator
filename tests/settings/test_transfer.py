"""Import/export between a directory of files and the settings service."""
import shutil
from pathlib import Path

import pytest
import yaml

from src.settings.errors import NotConfigured, SettingsInvalid
from src.settings.transfer import ImportFailed, export_dir, import_dir, unknown_keys
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
