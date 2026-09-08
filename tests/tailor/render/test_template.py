from src.tailor.models import (
    Bullet, Experience, Project, Education, Volunteer, ResumeContent, Skill,
    FitAnalysis, TailorResult, TailoredBullet, TailoredExperience,
)
from src.tailor.render.assemble import assemble_render_doc
from src.tailor.render.template import render_html


def _doc():
    content = ResumeContent(
        name="Jane Example",
        contact={"phone": "555", "email": "s@x.io", "website": "x.io", "github": "gh/s"},
        skills=[Skill(name="Go", category="Languages"), Skill(name="Angular", category="Frontend")],
        experiences=[Experience(id="exp-1", company="Acme Corp", role="Engineer", start="Mar 2021",
                                end="Present", bullets=[Bullet(id="exp-1-b1", text="orig")])],
        projects=[Project(id="proj-1", name="Foo", subtitle="Go + React",
                          dates="2024 – Present", bullets=[Bullet(id="proj-1-b1", text="proj work")])],
        education=[Education(degree="B.S. Computer Science", institution="State University", dates="2016 – 2020")],
        volunteer=[Volunteer(role="Mentor", org="Local Code Club", dates="2022 – Present")],
    )
    t = TailorResult(fit=FitAnalysis(), experiences=[TailoredExperience(experience_id="exp-1",
            bullets=[TailoredBullet(source_bullet_id="exp-1-b1", text="tailored exp bullet")])],
            skills_ordered=[], summary_placeholder="[S]", cover_letter="", is_fallback=False)
    return assemble_render_doc(content, t)


def test_template_renders_all_sections():
    html = render_html(_doc())
    for needle in ["Jane Example", "tailored exp bullet", "Acme Corp", "Foo",
                   "proj work", "State University", "Local Code Club", "Languages", "Go", "Angular", "#0f766e", "#5fa39c"]:
        assert needle in html, f"missing {needle!r}"


def test_template_has_no_summary_section():
    assert "summary" not in render_html(_doc()).lower()


def test_template_contact_uses_teal_separators():
    html = render_html(_doc())
    assert 's@x.io' in html and 'class="sep"' in html


def test_template_escapes_special_chars_in_content():
    from src.tailor.models import (Bullet, Experience, ResumeContent,
                                    FitAnalysis, TailorResult, TailoredExperience, TailoredBullet)
    txt = "Cut latency <1s & raised p99"
    content = ResumeContent(name="N", contact={}, skills=[],
        experiences=[Experience(id="e", company="C", role="R", start="", end="",
                                bullets=[Bullet(id="e-b1", text=txt)])], projects=[])
    t = TailorResult(fit=FitAnalysis(),
        experiences=[TailoredExperience(experience_id="e",
            bullets=[TailoredBullet(source_bullet_id="e-b1", text=txt)])],
        skills_ordered=[], summary_placeholder="[S]", cover_letter="", is_fallback=False)
    html = render_html(assemble_render_doc(content, t))
    assert "&lt;1s" in html and "&amp;" in html   # special chars escaped
    assert "<1s" not in html                       # raw < must not leak as a stray tag


def test_uploaded_template_is_sandboxed(tmp_path):
    from src.tailor.render.registry import pack_info
    (tmp_path / "template.html.j2").write_text(
        "{{ doc.__class__.__init__.__globals__ }}"
    )
    import pytest
    from jinja2.exceptions import SecurityError
    with pytest.raises(SecurityError):
        render_html(_doc(), pack_info(tmp_path, source="upload"))
