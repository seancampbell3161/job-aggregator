from pathlib import Path

from src.tailor.render.registry import (
    DEFAULT_SLUG,
    builtin_slugs,
    get_template,
    list_templates,
    pack_info,
    user_templates_dir,
)


def test_builtins_discovered():
    slugs = {t.slug for t in list_templates()}
    assert "classic" in slugs
    t = get_template("classic")
    assert t.name == "Classic"
    assert t.source == "builtin"
    assert (t.path / "template.html.j2").exists()
    assert "classic" in builtin_slugs()


def test_user_dir_merged_and_dot_dirs_skipped(tmp_path, monkeypatch):
    monkeypatch.setenv("JOB_AGG_TEMPLATES_DIR", str(tmp_path))
    (tmp_path / "mine").mkdir()
    (tmp_path / "mine" / "template.html.j2").write_text("<html>{{ doc.name }}</html>")
    (tmp_path / "mine" / "meta.yaml").write_text("name: Mine\nsource: upload\n")
    (tmp_path / ".pending").mkdir()
    (tmp_path / ".pending" / "template.html.j2").write_text("x")
    (tmp_path / "no-template-file").mkdir()
    slugs = {t.slug for t in list_templates()}
    assert "mine" in slugs
    assert ".pending" not in slugs and "no-template-file" not in slugs
    assert get_template("mine").name == "Mine"


def test_meta_optional_name_defaults_to_slug(tmp_path, monkeypatch):
    monkeypatch.setenv("JOB_AGG_TEMPLATES_DIR", str(tmp_path))
    (tmp_path / "bare").mkdir()
    (tmp_path / "bare" / "template.html.j2").write_text("<html></html>")
    assert get_template("bare").name == "bare"


def test_missing_slug_falls_back_to_classic(tmp_path, monkeypatch):
    monkeypatch.setenv("JOB_AGG_TEMPLATES_DIR", str(tmp_path))
    assert get_template("nope").slug == DEFAULT_SLUG


def test_user_templates_dir_default(monkeypatch):
    monkeypatch.delenv("JOB_AGG_TEMPLATES_DIR", raising=False)
    assert user_templates_dir() == Path("resume/templates")


def test_pack_info_for_arbitrary_dir(tmp_path):
    (tmp_path / "template.html.j2").write_text("<html></html>")
    info = pack_info(tmp_path, source="docx-import")
    assert info.slug == tmp_path.name and info.source == "docx-import"
