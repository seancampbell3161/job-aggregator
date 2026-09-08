from src.tailor.models import (
    Bullet, Experience, ResumeContent, Skill,
    EvidenceBank, EvidenceProject, EvidenceMetric,
)
from src.tailor.prompts import POLICY_PROMPT, serialize_content, serialize_evidence


def test_policy_prompt_encodes_no_fabrication_rules():
    p = POLICY_PROMPT.lower()
    # the load-bearing guardrails must all be present
    assert "fit" in p
    assert "never" in p and "skill" in p          # never add an unevidenced skill
    assert "ticket" in p                           # cite ticket refs for metrics
    assert "summary" in p and "placeholder" in p   # leave summary as a placeholder
    assert "source_bullet_id" in POLICY_PROMPT     # output references bullet ids


def test_serialize_content_includes_ids_and_skills():
    c = ResumeContent(
        name="S", contact={}, skills=[Skill(name="C#/.NET", category="language")],
        experiences=[Experience(id="exp-1", company="Acme", role="SSE", start="", end="",
                                bullets=[Bullet(id="exp-1-b1", text="did X")])],
        projects=[],
    )
    s = serialize_content(c)
    assert "exp-1-b1" in s and "C#/.NET" in s


def test_serialize_evidence_includes_ticket_refs():
    bank = EvidenceBank(projects=[EvidenceProject(
        key="acme", summary="s", metrics=[EvidenceMetric(claim="Cut 40%", ticket_refs=["JIRA-1"])])])
    assert "JIRA-1" in serialize_evidence(bank)


def test_policy_prompt_encodes_project_skill_and_budget_rules():
    p = POLICY_PROMPT
    assert '"project_id"' in p                 # projects in the output schema
    assert '"projects"' in p
    assert "prune" in p.lower()                # light-prune skills rule
    assert "one" in p.lower() and "page" in p.lower()   # one-page budget rule
    # existing no-fabrication guardrails still present
    assert "source_bullet_id" in p and "skills_ordered" in p


def test_policy_prompt_encodes_rewrite_all_ranked_semantics():
    p = POLICY_PROMPT
    assert "EVERY bullet" in p                       # rewrite-all: no subset selection
    assert "most-relevant-to-this-JD first" in p     # rank within each entry
    assert "trims from the TAIL" in p                # renderer trim contract stated to the model
    assert "pick the strongest" not in p             # old subset-selection wording is gone
