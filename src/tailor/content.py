"""Load + validate the structured résumé (content.json).

Validation fails LOUD (raises ValueError) — a malformed résumé must never be
silently tailored into garbage. Stable ids on experiences/bullets are required
because the TailorResult references selections by id."""

from __future__ import annotations

import json
from pathlib import Path

from src.tailor.models import (
    Bullet, Education, Experience, Project, ResumeContent, Skill, Volunteer,
)


def _require(obj: dict, key: str, ctx: str) -> object:
    if key not in obj:
        raise ValueError(f"{ctx}: missing required key '{key}'")
    return obj[key]


def _parse_bullets(items: list, *, seen: set[str], ctx: str) -> list[Bullet]:
    bullets: list[Bullet] = []
    for b in items:
        if "id" not in b:
            raise ValueError(f"{ctx} bullet missing 'id'")
        if b["id"] in seen:
            raise ValueError(f"duplicate bullet id '{b['id']}'")
        seen.add(b["id"])
        bullets.append(Bullet(
            id=b["id"], text=_require(b, "text", f"bullet {b['id']}"),
            tags=list(b.get("tags", [])), metric_bearing=bool(b.get("metric_bearing", False)),
            evidence_refs=list(b.get("evidence_refs", [])),
        ))
    return bullets


def parse_content(text: str) -> ResumeContent:
    raw = json.loads(text)
    if not isinstance(raw, dict):
        raise ValueError("content.json: top level must be a JSON object")

    skills = [
        Skill(name=_require(s, "name", "skill"), category=s.get("category", ""),
              tags=list(s.get("tags", [])))
        for s in _require(raw, "skills", "content.json")
    ]
    experiences: list[Experience] = []
    seen_bullet_ids: set[str] = set()
    for e in _require(raw, "experiences", "content.json"):
        if "id" not in e:
            raise ValueError("experience missing 'id'")
        bullets = _parse_bullets(e.get("bullets", []), seen=seen_bullet_ids, ctx=f"experience {e['id']}")
        experiences.append(Experience(
            id=e["id"], company=_require(e, "company", f"experience {e['id']}"),
            role=_require(e, "role", f"experience {e['id']}"),
            start=e.get("start", ""), end=e.get("end", ""), bullets=bullets,
        ))

    projects: list[Project] = []
    for p in raw.get("projects", []):
        projects.append(Project(
            id=_require(p, "id", "project"), name=_require(p, "name", "project"),
            subtitle=p.get("subtitle", ""), dates=p.get("dates", ""),
            bullets=_parse_bullets(p.get("bullets", []), seen=seen_bullet_ids, ctx=f"project {p.get('id','?')}"),
        ))

    education = [
        Education(degree=_require(ed, "degree", "education"),
                  institution=_require(ed, "institution", "education"), dates=ed.get("dates", ""))
        for ed in raw.get("education", [])
    ]
    volunteer = [
        Volunteer(role=_require(v, "role", "volunteer"), org=_require(v, "org", "volunteer"),
                  dates=v.get("dates", ""))
        for v in raw.get("volunteer", [])
    ]
    return ResumeContent(
        name=_require(raw, "name", "content.json"),
        contact=dict(raw.get("contact", {})),
        skills=skills, experiences=experiences, projects=projects,
        education=education, volunteer=volunteer,
    )


def load_content(path: Path | str) -> ResumeContent:
    return parse_content(Path(path).read_text())
