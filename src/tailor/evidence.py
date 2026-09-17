"""Load the pre-digested Jira evidence bank (evidence.json).

Small enough (~hundreds of tickets digested into a few projects) to pass in
full in the prompt — no retrieval. Serves as the citation layer: every metric
the model claims must cite a ticket_ref that appears here."""

from __future__ import annotations

import json
from pathlib import Path

from src.tailor.models import EvidenceAchievement, EvidenceBank, EvidenceMetric, EvidenceProject


def parse_evidence(text: str) -> EvidenceBank:
    raw = json.loads(text)
    if not isinstance(raw, dict) or "projects" not in raw:
        raise ValueError("evidence.json: missing required key 'projects'")
    projects = []
    for p in raw["projects"]:
        metrics = [EvidenceMetric(claim=m.get("claim", ""), ticket_refs=list(m.get("ticket_refs", [])))
                   for m in p.get("metrics", [])]
        achievements = [EvidenceAchievement(text=a.get("text", ""), ticket_refs=list(a.get("ticket_refs", [])))
                        for a in p.get("achievements", [])]
        projects.append(EvidenceProject(
            key=p.get("key", ""), summary=p.get("summary", ""),
            metrics=metrics, achievements=achievements,
        ))
    return EvidenceBank(projects=projects)


def load_evidence(path: Path | str) -> EvidenceBank:
    return parse_evidence(Path(path).read_text())
