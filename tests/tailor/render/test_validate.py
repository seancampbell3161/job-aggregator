from src.tailor.content import load_content
from src.tailor.render.validate import validate_pack
from tests.conftest import requires_weasyprint

CONTENT = load_content("resume/content.example.json")


@requires_weasyprint
def test_valid_minimal_pack_passes(tmp_path):
    (tmp_path / "template.html.j2").write_text(
        "<!DOCTYPE html><html><body>{{ doc.name }}"
        "{% for e in doc.experiences %}<p>{{ e.role }}</p>{% endfor %}</body></html>")
    assert validate_pack(tmp_path, CONTENT) is None


def test_bad_jinja_reports_error(tmp_path):
    (tmp_path / "template.html.j2").write_text("{% for %}")
    err = validate_pack(tmp_path, CONTENT)
    assert err is not None and "template" in err.lower()


def test_sandbox_escape_reports_error(tmp_path):
    (tmp_path / "template.html.j2").write_text("{{ doc.__class__.__init__.__globals__ }}")
    assert validate_pack(tmp_path, CONTENT) is not None


def test_missing_template_file_reports_error(tmp_path):
    assert validate_pack(tmp_path, CONTENT) is not None
