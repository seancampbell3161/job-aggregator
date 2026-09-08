"""Assemble a render document from résumé content + tailored output, and
deterministically fit it to one page.

Assembly is content-driven: entries render in content.json order (the model
never controls job-history order) and join tailored bullets by entry id; an
entry with no tailored counterpart renders its original bullets, which is how
a fallback TailorResult renders the untailored résumé. Skills stay flat in the
content model (Skill.name + Skill.category); the categorized rows the baseline
renders are DERIVED here, grouped by category in first-appearance order, with
tokens reordered by the engine's skills_ordered ranking and rows reordered by
JD relevance (first category pinned; see group_and_reorder_skills)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from src.tailor.models import Education, ResumeContent, Skill, TailorResult, Volunteer


@dataclass(frozen=True)
class SkillRow:
    label: str
    tokens: list[str]


def group_and_reorder_skills(skills: list[Skill], skills_ordered: list[str]) -> list[SkillRow]:
    """Group skills by category into rows, showing ALL of them — skills are
    pass-through for breadth and ATS keyword coverage. When `skills_ordered` is
    non-empty it reorders tokens WITHIN each row (ranked first, in ranked order;
    the rest keep original order) and reorders the ROWS by JD relevance: the
    first content category (Languages) is always pinned on top; the remaining
    rows sort by the best rank any of their skills received; rows with no
    ranked skill keep their original relative order after all ranked rows.
    Empty `skills_ordered` (a fallback TailorResult) leaves rows and tokens in
    original order. Skills and categories are never dropped."""
    rank = {name.lower(): i for i, name in enumerate(skills_ordered)}
    unranked = len(rank)   # sort key for skills the engine didn't rank -> after ranked ones
    groups: dict[str, list[str]] = {}
    for s in skills:
        groups.setdefault(s.category, []).append(s.name)
    rows = [
        SkillRow(label=label, tokens=sorted(tokens, key=lambda t: rank.get(t.lower(), unranked)))
        for label, tokens in groups.items()
    ]
    if not rank or len(rows) < 2:
        return rows

    def best(row: SkillRow) -> int:
        return min(rank.get(t.lower(), unranked) for t in row.tokens)

    return [rows[0], *sorted(rows[1:], key=best)]   # stable sort: unranked rows keep relative order


@dataclass(frozen=True)
class RenderExperience:
    company: str
    role: str
    dates: str
    bullets: list[str]


@dataclass(frozen=True)
class RenderProject:
    name: str
    subtitle: str
    dates: str
    bullets: list[str]


@dataclass
class RenderDoc:
    name: str
    contact_items: list[str]
    skill_rows: list[SkillRow]
    experiences: list[RenderExperience]
    projects: list[RenderProject]
    education: list[Education]
    volunteer: list[Volunteer]


_CONTACT_ORDER = ("phone", "email", "website", "github")


def _dates(start: str, end: str) -> str:
    parts = [p for p in (start, end) if p]
    return " – ".join(parts)


def assemble_render_doc(content: ResumeContent, tailored: TailorResult) -> RenderDoc:
    tailored_exps = {te.experience_id: te for te in tailored.experiences}
    experiences: list[RenderExperience] = []
    for ce in content.experiences:
        te = tailored_exps.get(ce.id)
        bullets = [b.text for b in te.bullets] if te and te.bullets else [b.text for b in ce.bullets]
        experiences.append(RenderExperience(
            company=ce.company, role=ce.role, dates=_dates(ce.start, ce.end), bullets=bullets))

    tailored_projs = {tp.project_id: tp for tp in tailored.projects}
    projects: list[RenderProject] = []
    for cp in content.projects:
        tp = tailored_projs.get(cp.id)
        bullets = [b.text for b in tp.bullets] if tp and tp.bullets else [b.text for b in cp.bullets]
        projects.append(RenderProject(
            name=cp.name, subtitle=cp.subtitle, dates=cp.dates, bullets=bullets))

    contact_items = [content.contact[k] for k in _CONTACT_ORDER if content.contact.get(k)]
    return RenderDoc(
        name=content.name, contact_items=contact_items,
        skill_rows=group_and_reorder_skills(content.skills, tailored.skills_ordered),
        experiences=experiences, projects=projects,
        education=list(content.education), volunteer=list(content.volunteer),
    )


def apply_bullet_caps(doc: "RenderDoc", *, max_experience: int | None,
                      max_project: int | None) -> list[str]:
    """Deterministic per-entry caps applied before page fitting. Keeps the HEAD
    of each bullet list — the engine emits bullets ranked by JD relevance, so
    the head is the best. Returns the dropped texts for reporting."""
    dropped: list[str] = []
    if max_experience is not None:
        for e in doc.experiences:
            dropped.extend(e.bullets[max_experience:])
            del e.bullets[max_experience:]
    if max_project is not None:
        for p in doc.projects:
            dropped.extend(p.bullets[max_project:])
            del p.bullets[max_project:]
    return dropped


def _drop_one_trailing_bullet(doc: "RenderDoc", min_bullets: int) -> str | None:
    """Drop the lowest-priority trailing bullet (projects before experiences),
    never taking a kept entry below min_bullets. Returns the dropped text, or
    None if nothing can be trimmed."""
    for proj in reversed(doc.projects):
        if len(proj.bullets) > min_bullets:
            return proj.bullets.pop()
    for exp in reversed(doc.experiences):
        if len(exp.bullets) > min_bullets:
            return exp.bullets.pop()
    return None


def fit_to_pages(doc: "RenderDoc", render_fn: Callable[["RenderDoc"], int], *,
                 max_pages: int = 1, min_bullets: int = 1) -> tuple["RenderDoc", list[str], str | None]:
    """Trim trailing bullets (projects first, then experiences) until render_fn
    reports <= max_pages. When trimming bottoms out at the min_bullets floor and
    the doc still overflows, deliver it anyway with a warning — a usable-but-long
    PDF beats a dead deep-link at apply time."""
    trimmed: list[str] = []
    while render_fn(doc) > max_pages:
        dropped = _drop_one_trailing_bullet(doc, min_bullets)
        if dropped is None:
            unit = "page" if max_pages == 1 else "pages"
            return doc, trimmed, (
                f"content still exceeds {max_pages} {unit} after trimming — "
                f"raise max pages or lower min bullets"
            )
        trimmed.append(dropped)
    return doc, trimmed, None
