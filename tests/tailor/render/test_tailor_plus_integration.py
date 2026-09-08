import pytest

pytestmark = pytest.mark.integration
from tests.conftest import WEASYPRINT_UNAVAILABLE

if WEASYPRINT_UNAVAILABLE is not None:  # OSError from cffi, not ImportError
    pytest.skip(f"needs WeasyPrint + native libs ({WEASYPRINT_UNAVAILABLE})",
                allow_module_level=True)

from src.tailor.content import load_content
from src.tailor.models import (FitAnalysis, TailorResult, TailoredExperience,
                               TailoredProject, TailoredBullet)
from src.tailor.render import render_resume


def test_real_resume_renders_one_page_with_tailored_projects_and_all_skills():
    c = load_content("resume/content.json")
    exp0 = c.experiences[0]
    proj0 = c.projects[0]
    skills = [s.name for s in c.skills][:8]   # engine ranks a subset; renderer still shows all
    t = TailorResult(
        fit=FitAnalysis(),
        experiences=[TailoredExperience(experience_id=exp0.id,
            bullets=[TailoredBullet(source_bullet_id=b.id, text=b.text) for b in exp0.bullets])],
        projects=[TailoredProject(project_id=proj0.id,
            bullets=[TailoredBullet(source_bullet_id=b.id, text=b.text) for b in proj0.bullets])],
        skills_ordered=skills, summary_placeholder="[S]", cover_letter="")
    result = render_resume(c, t)
    assert result.pdf[:4] == b"%PDF"
