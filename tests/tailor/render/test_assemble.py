from src.tailor.models import (
    Bullet, Experience, Project, Education, Volunteer, ResumeContent, Skill,
    FitAnalysis, TailorResult, TailoredBullet, TailoredExperience, TailoredProject,
)
from src.tailor.render.assemble import assemble_render_doc, apply_bullet_caps, RenderDoc, RenderExperience, RenderProject


def _content():
    return ResumeContent(
        name="Jane Example",
        contact={"phone": "555", "email": "s@x.io", "website": "x.io", "github": "gh/s"},
        skills=[Skill(name="Go", category="Languages"), Skill(name="C#/.NET", category="Languages")],
        experiences=[Experience(id="exp-1", company="Acme Corp", role="Engineer", start="Mar 2021",
                                end="Present", bullets=[Bullet(id="exp-1-b1", text="orig bullet")])],
        projects=[Project(id="proj-1", name="Foo", subtitle="Go + React",
                          dates="2024 – Present", bullets=[Bullet(id="proj-1-b1", text="proj bullet")])],
        education=[Education(degree="B.S. Computer Science", institution="State University", dates="2016 – 2020")],
        volunteer=[Volunteer(role="Mentor", org="Local Code Club", dates="2022 – Present")],
    )


def _tailored(**kw):
    base = dict(fit=FitAnalysis(), experiences=[], skills_ordered=[], summary_placeholder="[S]",
                cover_letter="", is_fallback=False)
    base.update(kw)
    return TailorResult(**base)


def test_assemble_uses_tailored_experience_bullets_with_content_meta():
    t = _tailored(experiences=[TailoredExperience(experience_id="exp-1",
                  bullets=[TailoredBullet(source_bullet_id="exp-1-b1", text="tailored bullet")])])
    doc = assemble_render_doc(_content(), t)
    assert doc.experiences[0].company == "Acme Corp" and doc.experiences[0].role == "Engineer"
    assert doc.experiences[0].dates == "Mar 2021 – Present"
    assert doc.experiences[0].bullets == ["tailored bullet"]


def test_assemble_falls_back_to_content_bullets_when_no_tailored():
    doc = assemble_render_doc(_content(), _tailored(experiences=[]))   # e.g. fallback result
    assert doc.experiences[0].bullets == ["orig bullet"]


def test_assemble_projects_education_volunteer_contact_static():
    doc = assemble_render_doc(_content(), _tailored())
    assert doc.projects[0].name == "Foo" and doc.projects[0].bullets == ["proj bullet"]
    assert doc.education[0].institution == "State University"
    assert doc.volunteer[0].org == "Local Code Club"
    assert doc.contact_items == ["555", "s@x.io", "x.io", "gh/s"]


def test_assemble_reorders_skills_by_ranking():
    # skills are pass-through (all shown); the ranking only reorders within a category
    doc = assemble_render_doc(_content(), _tailored(skills_ordered=["C#/.NET", "Go"]))
    assert doc.skill_rows[0].tokens == ["C#/.NET", "Go"]


def test_assemble_uses_tailored_project_bullets_with_content_meta():
    t = _tailored(projects=[TailoredProject(project_id="proj-1",
                  bullets=[TailoredBullet(source_bullet_id="proj-1-b1", text="tailored proj bullet")])])
    doc = assemble_render_doc(_content(), t)
    assert doc.projects[0].name == "Foo" and doc.projects[0].subtitle == "Go + React"
    assert doc.projects[0].bullets == ["tailored proj bullet"]


def test_assemble_falls_back_to_static_projects_when_no_tailored():
    doc = assemble_render_doc(_content(), _tailored(projects=[]))   # fallback
    assert doc.projects[0].bullets == ["proj bullet"]   # the content's own project bullet


def test_assemble_entry_order_follows_content_not_model():
    c = ResumeContent(
        name="N", contact={}, skills=[],
        experiences=[
            Experience(id="e-new", company="New Co", role="R", start="2023", end="present",
                       bullets=[Bullet(id="e-new-b1", text="n1")]),
            Experience(id="e-old", company="Old Co", role="R", start="2019", end="2023",
                       bullets=[Bullet(id="e-old-b1", text="o1")]),
        ],
        projects=[Project(id="p-1", name="P1", bullets=[Bullet(id="p-1-b1", text="p1")]),
                  Project(id="p-2", name="P2", bullets=[Bullet(id="p-2-b1", text="p2")])],
    )
    t = _tailored(
        experiences=[   # model emits entries in the WRONG order — must not matter
            TailoredExperience(experience_id="e-old",
                bullets=[TailoredBullet(source_bullet_id="e-old-b1", text="t-o1")]),
            TailoredExperience(experience_id="e-new",
                bullets=[TailoredBullet(source_bullet_id="e-new-b1", text="t-n1")]),
        ],
        projects=[
            TailoredProject(project_id="p-2",
                bullets=[TailoredBullet(source_bullet_id="p-2-b1", text="t-p2")]),
            TailoredProject(project_id="p-1",
                bullets=[TailoredBullet(source_bullet_id="p-1-b1", text="t-p1")]),
        ])
    doc = assemble_render_doc(c, t)
    assert [e.company for e in doc.experiences] == ["New Co", "Old Co"]   # content order wins
    assert [e.bullets for e in doc.experiences] == [["t-n1"], ["t-o1"]]
    assert [p.name for p in doc.projects] == ["P1", "P2"]
    assert [p.bullets for p in doc.projects] == [["t-p1"], ["t-p2"]]


def test_assemble_entry_with_empty_tailored_bullets_renders_original():
    t = _tailored(experiences=[TailoredExperience(experience_id="exp-1", bullets=[])])
    doc = assemble_render_doc(_content(), t)
    assert doc.experiences[0].bullets == ["orig bullet"]


def _doc_with_bullets(exp_bullets, proj_bullets):
    return RenderDoc(
        name="N", contact_items=[], skill_rows=[],
        experiences=[RenderExperience(company="C", role="R", dates="", bullets=list(exp_bullets))],
        projects=[RenderProject(name="P", subtitle="", dates="", bullets=list(proj_bullets))],
        education=[], volunteer=[],
    )


def test_caps_keep_head_and_report_dropped():
    doc = _doc_with_bullets(["e1", "e2", "e3"], ["p1", "p2"])
    dropped = apply_bullet_caps(doc, max_experience=2, max_project=1)
    assert doc.experiences[0].bullets == ["e1", "e2"]
    assert doc.projects[0].bullets == ["p1"]
    assert dropped == ["e3", "p2"]


def test_none_caps_are_noops():
    doc = _doc_with_bullets(["e1", "e2"], ["p1"])
    assert apply_bullet_caps(doc, max_experience=None, max_project=None) == []
    assert doc.experiences[0].bullets == ["e1", "e2"]
