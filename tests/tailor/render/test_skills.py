from src.tailor.models import Skill
from src.tailor.render.assemble import SkillRow, group_and_reorder_skills


def _skills():
    return [
        Skill(name="Go", category="Languages"),
        Skill(name="C#/.NET", category="Languages"),
        Skill(name="SQL", category="Languages"),
        Skill(name="React", category="Frontend"),
        Skill(name="Angular", category="Frontend"),
    ]


def test_shows_all_skills_with_selected_reordered_first():
    rows = group_and_reorder_skills(_skills(), ["C#/.NET", "Angular", "Go"])
    assert [r.label for r in rows] == ["Languages", "Frontend"]   # category order fixed
    langs = next(r for r in rows if r.label == "Languages")
    front = next(r for r in rows if r.label == "Frontend")
    # ALL skills shown; ranked ones first (C#/.NET, Go), unranked (SQL) keeps original order
    assert langs.tokens == ["C#/.NET", "Go", "SQL"]
    assert front.tokens == ["Angular", "React"]   # Angular ranked first; React after


def test_empty_selection_shows_all_skills():
    rows = group_and_reorder_skills(_skills(), [])   # fallback TailorResult
    assert [r.label for r in rows] == ["Languages", "Frontend"]
    assert rows[0].tokens == ["Go", "C#/.NET", "SQL"]   # all, original order


def test_category_with_no_selected_skill_is_kept():
    rows = group_and_reorder_skills(_skills(), ["Go"])   # only a Languages skill ranked
    assert [r.label for r in rows] == ["Languages", "Frontend"]   # Frontend NOT dropped
    langs = next(r for r in rows if r.label == "Languages")
    front = next(r for r in rows if r.label == "Frontend")
    assert langs.tokens == ["Go", "C#/.NET", "SQL"]   # Go first; rest original order
    assert front.tokens == ["React", "Angular"]       # untouched, original order


def test_invented_skill_in_selection_is_ignored():
    rows = group_and_reorder_skills(_skills(), ["Kubernetes", "SQL"])   # Kubernetes not a skill
    langs = next(r for r in rows if r.label == "Languages")
    front = next(r for r in rows if r.label == "Frontend")
    assert langs.tokens == ["SQL", "Go", "C#/.NET"]   # SQL ranked first; Kubernetes ignored
    assert front.tokens == ["React", "Angular"]       # all frontend skills still shown


def _four_cat_skills():
    return [
        Skill(name="Go", category="Languages"),
        Skill(name="React", category="Frontend"),
        Skill(name="LangChain", category="AI"),
        Skill(name="Docker", category="Infrastructure"),
    ]


def test_rows_reorder_by_best_rank_pinned_first():
    rows = group_and_reorder_skills(_four_cat_skills(), ["LangChain", "React"])
    # Languages pinned; AI (best rank 0) before Frontend (1); Infrastructure unranked -> last
    assert [r.label for r in rows] == ["Languages", "AI", "Frontend", "Infrastructure"]


def test_first_category_pinned_even_when_unranked():
    rows = group_and_reorder_skills(_four_cat_skills(), ["React"])
    # without the pin Frontend would lead; Languages stays on top by convention
    assert [r.label for r in rows] == ["Languages", "Frontend", "AI", "Infrastructure"]


def test_empty_ranking_keeps_original_row_order():
    rows = group_and_reorder_skills(_four_cat_skills(), [])   # fallback TailorResult
    assert [r.label for r in rows] == ["Languages", "Frontend", "AI", "Infrastructure"]
