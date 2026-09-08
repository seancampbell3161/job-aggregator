from src.tailor.models import (
    Bullet, Experience, ResumeContent, Skill,
    EvidenceBank, EvidenceProject, EvidenceMetric, EvidenceAchievement,
    TailorResult, FitAnalysis, TailoredExperience, TailoredBullet, TailoredProject,
    render_input_dict, result_from_render_input,
)


def _content() -> ResumeContent:
    return ResumeContent(
        name="Jane Example",
        contact={"email": "x@y.z"},
        skills=[Skill(name="C#/.NET", category="language", tags=["primary"])],
        experiences=[
            Experience(id="exp-acme", company="Acme", role="SSE", start="2021-03", end="present",
                       bullets=[Bullet(id="exp-acme-b1", text="did X", evidence_refs=["JIRA-1"])]),
        ],
        projects=[],
    )


def test_bullet_ids_collects_all_bullet_ids():
    assert _content().bullet_ids() == {"exp-acme-b1"}


def test_evidence_ticket_refs_collects_all_refs():
    bank = EvidenceBank(projects=[
        EvidenceProject(key="acme", summary="s",
                        metrics=[EvidenceMetric(claim="c", ticket_refs=["JIRA-1"])],
                        achievements=[EvidenceAchievement(text="a", ticket_refs=["JIRA-2"])]),
    ])
    assert bank.ticket_refs() == {"JIRA-1", "JIRA-2"}


def test_tailorresult_fallback_is_marked_and_empty():
    r = TailorResult.fallback()
    assert r.is_fallback is True
    assert r.experiences == []
    assert r.fit.matches == [] and r.fit.gaps == []
    assert "unavailable" in r.cover_letter.lower()


def test_project_has_bullets_and_subtitle():
    from src.tailor.models import Project, Bullet
    p = Project(id="proj-rc", name="Foo", subtitle="Go + React", dates="2024 – Present",
                bullets=[Bullet(id="proj-rc-b1", text="Four Go microservices")])
    assert p.bullets[0].id == "proj-rc-b1"
    assert p.subtitle == "Go + React" and p.dates == "2024 – Present"


def test_education_and_volunteer():
    from src.tailor.models import Education, Volunteer
    e = Education(degree="B.S. Computer Science", institution="State University", dates="2016 – 2020")
    v = Volunteer(role="Mentor", org="Local Code Club", dates="Feb 2022 – Present")
    assert e.institution == "State University" and v.org == "Local Code Club"


def test_resumecontent_education_volunteer_default_empty():
    from src.tailor.models import ResumeContent
    rc = ResumeContent(name="S", contact={}, skills=[], experiences=[])
    assert rc.education == [] and rc.volunteer == []


def test_tailored_project_and_result_projects_field():
    from src.tailor.models import TailoredProject, TailoredBullet, TailorResult
    tp = TailoredProject(project_id="proj-1",
                         bullets=[TailoredBullet(source_bullet_id="proj-1-b1", text="x")])
    assert tp.project_id == "proj-1" and tp.bullets[0].text == "x"
    assert TailorResult.fallback().projects == []


def test_project_bullet_ids_collects_project_bullets_only():
    from src.tailor.models import ResumeContent, Project, Experience, Bullet
    c = ResumeContent(
        name="N", contact={}, skills=[],
        experiences=[Experience(id="e", company="C", role="R", start="", end="",
                                bullets=[Bullet(id="exp-b1", text="t")])],
        projects=[Project(id="p1", name="P", bullets=[Bullet(id="p1-b1", text="t")])],
    )
    assert c.project_bullet_ids() == {"p1-b1"}        # project bullets only
    assert c.bullet_ids() == {"exp-b1"}               # experiences only (unchanged)


def _result():
    return TailorResult(
        fit=FitAnalysis(matches=["m"], gaps=["g"], overall="o"),
        experiences=[TailoredExperience(experience_id="e1", bullets=[
            TailoredBullet(source_bullet_id="b1", text="did x", evidence_refs=["J-1"])])],
        skills_ordered=["Python"],
        summary_placeholder="s",
        cover_letter="letter",
        projects=[TailoredProject(project_id="p1", bullets=[
            TailoredBullet(source_bullet_id="pb1", text="built y")])],
    )


def test_render_input_roundtrip():
    d = render_input_dict(_result())
    back = result_from_render_input(d)
    assert back.experiences == _result().experiences
    assert back.projects == _result().projects
    assert back.skills_ordered == ["Python"]
    assert back.cover_letter == ""            # not part of render input
    assert back.fit == FitAnalysis()
