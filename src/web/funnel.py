from __future__ import annotations

import math
from dataclasses import dataclass, replace

# Progression ladder a job advances through; terminal statuses are exits, not
# stages. A job's furthest stage is the max ladder position across its history
# plus current status — skipped stages count as passed through, backward moves
# are absorbed.
_LADDER = ("new", "interested", "applied", "interviewing", "offer")
_STAGE_IDX = {s: i for i, s in enumerate(_LADDER)}
NEW, INTERESTED, APPLIED, INTERVIEWING, OFFER = (_STAGE_IDX[s] for s in _LADDER)

# (node id, label, column) in render order; order within a column is also the
# vertical stacking order. Outcomes live in the final column so no ribbon ever
# runs intra-column or backward.
_NODES = (
    ("matches", "Matches", 0),
    ("interested", "Interested", 1),
    ("still_new", "Still new", 1),
    ("dismissed", "Dismissed", 1),
    ("applied", "Applied", 2),
    ("interviewing", "Interviewing", 3),
    ("awaiting", "Awaiting reply", 3),
    ("offer", "Offer", 4),
    ("rejected", "Rejected", 4),
    ("ghosted", "Ghosted", 4),
    ("withdrawn", "Withdrawn", 4),
)
_STAGE_NODE = ("matches", "interested", "applied", "interviewing", "offer")


@dataclass(frozen=True)
class FunnelNode:
    id: str
    label: str
    column: int
    count: int


@dataclass(frozen=True)
class FunnelLink:
    source: str
    target: str
    count: int


@dataclass(frozen=True)
class Funnel:
    nodes: list[FunnelNode]
    links: list[FunnelLink]


@dataclass(frozen=True)
class Rate:
    label: str
    noun: str   # denominator noun for the caption
    num: int
    den: int

    @property
    def pct(self) -> float | None:
        return (100.0 * self.num / self.den) if self.den else None

    @property
    def display(self) -> str:
        if self.pct is None:
            return "—"
        if self.pct and self.pct < 1:
            return f"{self.pct:.1f}%"
        return f"{round(self.pct)}%"

    @property
    def dash(self) -> float:
        """Filled arc length for the r=30 ring meter (circumference 188.5)."""
        return 0.0 if self.pct is None else round(188.5 * min(self.pct, 100.0) / 100.0, 1)

    @property
    def caption(self) -> str:
        return f"{self.num} of {self.den} {self.noun}" if self.den else f"no {self.noun} yet"


def _reduce(match: dict) -> tuple[int, str]:
    """(furthest progression stage index, current status) for one match."""
    current = match.get("status", "new")
    seen = {h.get("status") for h in (match.get("history") or []) if isinstance(h, dict)}
    seen.add(current)
    stages = [_STAGE_IDX[s] for s in seen if s in _STAGE_IDX]
    return (max(stages) if stages else NEW), current


def _exit_node(current: str, progression: int) -> str | None:
    if current == "rejected":
        return "rejected"
    if current == "ghosted":
        return "ghosted"
    if current == "dismissed":
        return "dismissed" if progression == NEW else "withdrawn"
    return None


def build_funnel(matches: list[dict]) -> Funnel:
    """Reduce notified matches to Sankey nodes + links. Stage nodes count
    everything that reached the stage; exit links leave the furthest stage
    reached; a job that reached Offer never draws an exit (the outcome column
    is final — a later rejected/declined stays visible in the table view);
    zero-count nodes/links are dropped."""
    node_counts: dict[str, int] = {node_id: 0 for node_id, _, _ in _NODES}
    link_counts: dict[tuple[str, str], int] = {}

    def add_link(src: str, dst: str) -> None:
        link_counts[(src, dst)] = link_counts.get((src, dst), 0) + 1

    node_counts["matches"] = len(matches)
    for match in matches:
        progression, current = _reduce(match)
        for stage in range(INTERESTED, progression + 1):
            node_counts[_STAGE_NODE[stage]] += 1
            add_link(_STAGE_NODE[stage - 1], _STAGE_NODE[stage])
        exit_id = None if progression >= OFFER else _exit_node(current, progression)
        if exit_id is not None:
            node_counts[exit_id] += 1
            add_link(_STAGE_NODE[progression], exit_id)
        elif progression == NEW and current == "new":
            node_counts["still_new"] += 1
            add_link("matches", "still_new")
        elif progression == APPLIED and current == "applied":
            node_counts["awaiting"] += 1
            add_link("applied", "awaiting")

    nodes = [
        FunnelNode(node_id, label, column, node_counts[node_id])
        for node_id, label, column in _NODES if node_counts[node_id] > 0
    ]
    links = [FunnelLink(s, t, c) for (s, t), c in link_counts.items()]
    return Funnel(nodes=nodes, links=links)


# Triage exits: the two ways a match leaves at the very first hop, without ever
# entering the application pipeline.
_TRIAGE_EXITS = ("dismissed", "still_new")


@dataclass(frozen=True)
class TriageSegment:
    id: str
    label: str
    count: int
    pct: float

    @property
    def share(self) -> str:
        if self.pct and self.pct < 1:
            return f"{self.pct:.1f}%"
        return f"{round(self.pct)}%"


@dataclass(frozen=True)
class Triage:
    total: int
    segments: list[TriageSegment]


def triage_split(funnel: Funnel) -> Triage | None:
    """The first hop as a 100%-stacked bar rather than a Sankey column.

    At the real data shape that hop is a ~94/2/4 split — three orders of
    magnitude across the single linear scale a Sankey has to share. Drawn as a
    Sankey column it set a scale that crushed every later stage to its 4px floor
    and stacked the whole live funnel into a 45px sliver, which is what put the
    labels in the lane the long-range ribbons travel through. A stacked bar shows
    the same split at true proportion in 26px, and leaves the Sankey a scale it
    can actually render."""
    counts = {n.id: n.count for n in funnel.nodes}
    total = counts.get("matches", 0)
    if not total:
        return None
    segments = [
        TriageSegment(node_id, label, count, round(100.0 * count / total, 1))
        for node_id, label, count in (
            ("pipeline", "In pipeline", _live_count(counts)),
            ("still_new", "Still new", counts.get("still_new", 0)),
            ("dismissed", "Dismissed", counts.get("dismissed", 0)),
        )
        if count > 0
    ]
    return Triage(total=total, segments=segments)


def _live_count(counts: dict[str, int]) -> int:
    return counts.get("matches", 0) - sum(counts.get(i, 0) for i in _TRIAGE_EXITS)


def pipeline_funnel(funnel: Funnel) -> Funnel:
    """`funnel` with the triage hop removed, for the Sankey to render.

    The triage exits drop out and `matches` becomes `In pipeline`, counting only
    what triage let through — which is exactly the sum of the links that survive,
    since every match either exits at triage or leaves `matches` by one of them.
    The remaining counts sit within one order of magnitude of each other, so one
    linear scale renders them all above the floors. See `triage_split`."""
    counts = {n.id: n.count for n in funnel.nodes}
    nodes = [
        FunnelNode("pipeline", "In pipeline", n.column, _live_count(counts))
        if n.id == "matches" else n
        for n in funnel.nodes
        if n.id not in _TRIAGE_EXITS
    ]
    links = [
        FunnelLink("pipeline" if l.source == "matches" else l.source, l.target, l.count)
        for l in funnel.links
        if l.target not in _TRIAGE_EXITS
    ]
    return Funnel(nodes=[n for n in nodes if n.count > 0], links=links)


def pipeline_rates(matches: list[dict]) -> list[Rate]:
    """Apply / interview / offer rates for the ring-meter tiles. Interview and
    offer rates use applications as the denominator."""
    total = len(matches)
    reached = [_reduce(m)[0] for m in matches]
    applied = sum(1 for p in reached if p >= APPLIED)
    interviewing = sum(1 for p in reached if p >= INTERVIEWING)
    offers = sum(1 for p in reached if p >= OFFER)
    return [
        Rate("Apply rate", "matches", applied, total),
        Rate("Interview rate", "applications", interviewing, applied),
        Rate("Offer rate", "applications", offers, applied),
    ]



# ---- Pipeline panel ---------------------------------------------------------
# The panel is three stacked bands rather than a Sankey. A Sankey earns its keep
# on many-to-many flow; this data is a linear funnel that barely narrows (70 →
# 66 → 63, ~90% retention) and then splits into terminal outcomes, which drew as
# three near-identical slabs carrying almost no information. Bars say the same
# thing in a third of the height with every number legible. The stage-level exit
# detail a Sankey would carry (interested → withdrawn vs applied → withdrawn)
# stays available in `Pipeline.flows`, rendered as the table view.

# Current status → (palette slug, display label) for the standing bar. Insertion
# order is the tie-break when two statuses have the same count, so the bar is
# deterministic; slugs are shared with the CSS custom properties.
_STANDING = {
    "offer": ("offer", "Offer"),
    "interviewing": ("interviewing", "Interviewing"),
    "applied": ("awaiting", "Awaiting reply"),
    "interested": ("interested", "Interested"),
    "rejected": ("rejected", "Rejected"),
    "ghosted": ("ghosted", "Ghosted"),
    "dismissed": ("withdrawn", "Withdrawn"),
}
_STANDING_ORDER = {status: i for i, status in enumerate(_STANDING)}
# Step 0 is every job triage let through; the rest are ladder rungs.
_STEP_LABELS = ("In pipeline", "Interested", "Applied", "Interviewing", "Offer")


def _share(num: int, den: int) -> str:
    if not den:
        return "—"
    pct = 100.0 * num / den
    return f"{pct:.1f}%" if pct and pct < 1 else f"{round(pct)}%"


@dataclass(frozen=True)
class FunnelStep:
    id: str
    label: str
    count: int
    width_pct: float          # bar width relative to the first step
    conversion: str | None    # share of the previous step; None on the first
    conversion_note: str | None


@dataclass(frozen=True)
class StandingSegment:
    id: str                   # palette slug, not the raw status
    label: str
    count: int
    pct: float

    @property
    def share(self) -> str:
        return f"{self.pct:.1f}%" if self.pct and self.pct < 1 else f"{round(self.pct)}%"


@dataclass(frozen=True)
class Flow:
    source: str               # display labels, ready for the table view
    target: str
    count: int


@dataclass(frozen=True)
class Pipeline:
    triage: Triage | None
    live: int                 # jobs triage let through — the steps/standing denominator
    steps: list[FunnelStep]
    standing: list[StandingSegment]
    flows: list[Flow]


def _is_triage_exit(progression: int, current: str) -> bool:
    """Left at the very first hop, never entering the application pipeline.

    Mirrors the two triage nodes `build_funnel` emits — still_new (untouched)
    and dismissed-from-new — so `live` here and `_live_count` there cannot
    drift apart."""
    return progression == NEW and current in ("new", "dismissed")


def _funnel_steps(live: list[tuple[int, str]]) -> list[FunnelStep]:
    reached = [sum(1 for progression, _ in live if progression >= i) for i in range(len(_LADDER))]
    first = reached[0]
    steps = []
    for i, count in enumerate(reached):
        steps.append(FunnelStep(
            id="pipeline" if i == 0 else _LADDER[i],
            label=_STEP_LABELS[i],
            count=count,
            width_pct=round(100.0 * count / first, 2) if first else 0.0,
            conversion=None if i == 0 else _share(count, reached[i - 1]),
            conversion_note=None if i == 0
            else f"{_share(count, reached[i - 1])} of {_STEP_LABELS[i - 1]}",
        ))
    return steps


def _standing(live: list[tuple[int, str]]) -> list[StandingSegment]:
    """Where the live jobs sit right now, by current status.

    Distinct from the steps above, which count the furthest stage each job ever
    reached: 5 jobs reached Interviewing, 3 are still there. The segments
    partition `live` exactly, so the bar always totals 100%."""
    tally: dict[str, int] = {}
    for _, current in live:
        tally[current] = tally.get(current, 0) + 1
    total = len(live)
    return [
        StandingSegment(
            id=_STANDING.get(status, ("other", status))[0],
            label=_STANDING.get(status, ("other", status.replace("_", " ").capitalize()))[1],
            count=count,
            pct=round(100.0 * count / total, 1),
        )
        for status, count in sorted(
            tally.items(), key=lambda kv: (-kv[1], _STANDING_ORDER.get(kv[0], len(_STANDING)))
        )
    ]


def build_pipeline(matches: list[dict]) -> Pipeline:
    """Everything the pipeline panel renders, from one pass over the matches."""
    funnel = build_funnel(matches)
    labels = {node_id: label for node_id, label, _ in _NODES} | {"pipeline": "In pipeline"}
    live = [pair for pair in (_reduce(m) for m in matches) if not _is_triage_exit(*pair)]
    return Pipeline(
        triage=triage_split(funnel),
        live=len(live),
        steps=_funnel_steps(live) if live else [],
        standing=_standing(live),
        flows=[
            Flow(labels[l.source], labels[l.target], l.count)
            for l in pipeline_funnel(funnel).links
        ],
    )
