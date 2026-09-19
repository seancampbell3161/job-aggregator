from src.tailor.models import (
    Bullet, Experience, ResumeContent, Skill,
    EvidenceBank, EvidenceProject, EvidenceMetric,
)
from src.tailor.prompts import (
    POLICY_PROMPT, RULE_4_WITH_EVIDENCE, build_system_text,
    serialize_content, serialize_evidence,
)


def _content():
    return ResumeContent(
        name="S", contact={}, skills=[Skill(name="Go", category="language")],
        experiences=[Experience(id="exp-1", company="Acme", role="SSE", start="", end="",
                                bullets=[Bullet(id="exp-1-b1", text="orig")])],
        projects=[],
    )


def test_policy_prompt_encodes_no_fabrication_rules():
    p = POLICY_PROMPT.lower()
    # the load-bearing guardrails must all be present
    assert "fit" in p
    assert "never" in p and "skill" in p          # never add an unevidenced skill
    # rule 4's citation text is no longer baked into POLICY_PROMPT itself --
    # build_system_text substitutes it in only when a bank exists — so the
    # ticket-citation guardrail is checked on the variant that carries it.
    assert "ticket" in RULE_4_WITH_EVIDENCE.lower()
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


_BANK = EvidenceBank(projects=[EvidenceProject(
    key="acme", summary="billing", metrics=[EvidenceMetric(claim="p95 cut", ticket_refs=["JIRA-1"])])])


def test_a_real_bank_keeps_todays_prompt():
    text = build_system_text(_content(), _BANK)
    assert "----- EVIDENCE BANK -----" in text
    assert "JIRA-1" in text
    assert "CITE EVIDENCE FOR METRICS" in text
    assert "NEVER INTRODUCE A NUMBER" not in text


def test_an_empty_bank_omits_the_evidence_section():
    """Assert on the section delimiter, not on the absence of `"projects"`:
    ResumeContent HAS a projects field, so serialize_content emits that key
    whatever the bank holds, and asserting its absence would fail against
    correct code."""
    text = build_system_text(_content(), EvidenceBank())
    assert "----- EVIDENCE BANK -----" not in text


def test_an_empty_bank_swaps_the_citation_rule_for_a_no_new_numbers_rule():
    """The load-bearing assertion of this task. Rule 4 as written is
    unsatisfiable with no bank, and a model that obeys it strips every metric
    from every bullet — handing back a worse résumé than the input, silently."""
    text = build_system_text(_content(), EvidenceBank())
    assert "CITE EVIDENCE FOR METRICS" not in text
    assert "NEVER INTRODUCE A NUMBER" in text
    assert "already in the CONTENT bullet" in text


def test_an_empty_bank_tells_the_model_the_other_rules_mean_content_only():
    """Rules 2 and 8 and the opening paragraph all say "CONTENT or EVIDENCE".
    Without this clause the prompt refers to a bank that is not there."""
    text = build_system_text(_content(), EvidenceBank())
    assert "no EVIDENCE bank" in text
