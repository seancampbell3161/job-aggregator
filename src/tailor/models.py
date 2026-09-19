"""Dataclasses for résumé tailoring.

Three groups:
- INPUT résumé content (``ResumeContent`` and parts), loaded from content.json.
- INPUT evidence bank (``EvidenceBank``), loaded from evidence.json.
- OUTPUT ``TailorResult`` — the contract sub-projects B (render) and C (hosted
  flow) consume. Frozen for safety; list fields are conventionally not mutated."""

from __future__ import annotations

from dataclasses import dataclass, field


# ----- input: structured résumé content -----
@dataclass(frozen=True)
class Skill:
    name: str
    category: str
    tags: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Bullet:
    id: str
    text: str
    tags: list[str] = field(default_factory=list)
    metric_bearing: bool = False
    evidence_refs: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Experience:
    id: str
    company: str
    role: str
    start: str
    end: str
    bullets: list[Bullet] = field(default_factory=list)


@dataclass(frozen=True)
class Project:
    id: str
    name: str
    subtitle: str = ""        # the tech line, e.g. "Go microservices + Next.js"
    dates: str = ""
    bullets: list[Bullet] = field(default_factory=list)


@dataclass(frozen=True)
class Education:
    degree: str
    institution: str
    dates: str = ""


@dataclass(frozen=True)
class Volunteer:
    role: str
    org: str
    dates: str = ""


@dataclass(frozen=True)
class ResumeContent:
    name: str
    contact: dict
    skills: list[Skill]
    experiences: list[Experience]
    projects: list[Project] = field(default_factory=list)
    education: list[Education] = field(default_factory=list)
    volunteer: list[Volunteer] = field(default_factory=list)

    def bullet_ids(self) -> set[str]:
        return {b.id for e in self.experiences for b in e.bullets}

    def project_bullet_ids(self) -> set[str]:
        return {b.id for p in self.projects for b in p.bullets}


# ----- input: evidence bank -----
@dataclass(frozen=True)
class EvidenceMetric:
    claim: str
    ticket_refs: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class EvidenceAchievement:
    text: str
    ticket_refs: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class EvidenceProject:
    key: str
    summary: str
    metrics: list[EvidenceMetric] = field(default_factory=list)
    achievements: list[EvidenceAchievement] = field(default_factory=list)


@dataclass(frozen=True)
class EvidenceBank:
    projects: list[EvidenceProject] = field(default_factory=list)

    def ticket_refs(self) -> set[str]:
        out: set[str] = set()
        for p in self.projects:
            for m in p.metrics:
                out.update(m.ticket_refs)
            for a in p.achievements:
                out.update(a.ticket_refs)
        return out


# ----- output: the TailorResult contract -----
@dataclass(frozen=True)
class TailoredBullet:
    source_bullet_id: str            # which content.json bullet this came from
    text: str                        # rewritten/selected for THIS posting
    evidence_refs: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class TailoredExperience:
    experience_id: str
    bullets: list[TailoredBullet] = field(default_factory=list)


@dataclass(frozen=True)
class TailoredProject:
    project_id: str
    bullets: list[TailoredBullet] = field(default_factory=list)


@dataclass(frozen=True)
class FitAnalysis:
    matches: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    overall: str = ""


@dataclass(frozen=True)
class TailorResult:
    fit: FitAnalysis
    experiences: list[TailoredExperience]
    skills_ordered: list[str]
    summary_placeholder: str
    cover_letter: str
    projects: list[TailoredProject] = field(default_factory=list)
    is_fallback: bool = False

    @classmethod
    def fallback(cls, reason: str = "") -> "TailorResult":
        # No reason: the pre-existing case, still right for a malformed or
        # empty provider response, where a retry might succeed. A given
        # reason means the failure is permanent for this configuration (e.g.
        # a provider's own max_tokens ceiling) — naming it beats sending the
        # user around a retry loop that can never succeed.
        cover_letter = (
            f"(Tailoring unavailable — {reason})" if reason
            else "(Tailoring unavailable — the LLM call failed. Retry shortly.)"
        )
        return cls(
            fit=FitAnalysis(),
            experiences=[],
            skills_ordered=[],
            summary_placeholder=SUMMARY_PLACEHOLDER,
            cover_letter=cover_letter,
            projects=[],
            is_fallback=True,
        )


SUMMARY_PLACEHOLDER = "[SUMMARY — write 2–3 lines in your own voice]"


def render_input_dict(result: TailorResult) -> dict:
    """The persisted render input: everything needed to re-render this run
    through a different template without re-calling the LLM."""
    from dataclasses import asdict
    return {
        "experiences": [asdict(e) for e in result.experiences],
        "projects": [asdict(p) for p in result.projects],
        "skills_ordered": result.skills_ordered,
        "summary_placeholder": result.summary_placeholder,
    }


def result_from_render_input(d: dict) -> TailorResult:
    return TailorResult(
        fit=FitAnalysis(),
        experiences=[
            TailoredExperience(experience_id=e["experience_id"],
                               bullets=[TailoredBullet(**b) for b in e.get("bullets", [])])
            for e in d.get("experiences", [])],
        skills_ordered=list(d.get("skills_ordered", [])),
        summary_placeholder=d.get("summary_placeholder", ""),
        cover_letter="",
        projects=[
            TailoredProject(project_id=p["project_id"],
                            bullets=[TailoredBullet(**b) for b in p.get("bullets", [])])
            for p in d.get("projects", [])],
    )
