"""Verdict-driven threshold analysis (pure — no IO).

Mines /audit rescue/confirm verdicts to recommend score_low and title-regex
adjustments. Verdicts exist only on already-suppressed/rejected rows, so the
score_low analysis can only ever recommend LOWERING the threshold (surfacing
false-suppressions), never raising it. Consumed by scripts/tune_thresholds.py."""
from __future__ import annotations

import re
import statistics
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True)
class ScoreLowRecommendation:
    current: int
    suggested: int | None          # None below the confidence gate
    rescued_scores: list[int]      # suppressed scores the user rescued
    confirmed_scores: list[int]    # suppressed scores the user confirmed
    errors_by_threshold: dict[int, int]
    sample_size: int
    confident: bool
    note: str


def analyze_score_low(
    suppressed_rows: list[dict], *, current: int, min_verdicts: int = 10
) -> ScoreLowRecommendation:
    rescued: list[int] = []
    confirmed: list[int] = []
    for row in suppressed_rows:
        verdict = row.get("audit_verdict")
        if verdict not in ("rescued", "confirmed_rejected"):
            continue
        score = row.get("score")
        if not isinstance(score, int) or isinstance(score, bool):
            continue  # sparse pre-mark_suppressed rows
        (rescued if verdict == "rescued" else confirmed).append(score)

    sample_size = len(rescued) + len(confirmed)
    errors_by_threshold = {
        t: sum(1 for s in rescued if s <= t) + sum(1 for s in confirmed if s > t)
        for t in range(0, 11)
    }

    if sample_size < min_verdicts or not rescued or not confirmed:
        note = (
            f"insufficient data: {sample_size} labeled suppressed verdict(s) "
            f"(need >= {min_verdicts}, with both rescued and confirmed present)"
        )
        return ScoreLowRecommendation(
            current, None, sorted(rescued), sorted(confirmed),
            errors_by_threshold, sample_size, False, note,
        )

    # min errors; ties → closest to current, then lower t
    best_t = min(range(0, 11), key=lambda t: (errors_by_threshold[t], abs(t - current), t))
    if best_t >= current:
        note = (
            f"no beneficial lowering found (best threshold {best_t} >= current {current}). "
            f"Note: verdicts exist only on suppressed rows (score <= {current}), so this tool "
            f"can only recommend LOWERING score_low, never raising it."
        )
    else:
        surfaced = sum(1 for s in rescued if best_t < s <= current)
        kept = sum(1 for s in confirmed if s <= best_t)
        note = (
            f"lowering score_low {current} -> {best_t} would have surfaced {surfaced} "
            f"rescued job(s) while keeping {kept} confirmed suppression(s)"
        )
    return ScoreLowRecommendation(
        current, best_t, sorted(rescued), sorted(confirmed),
        errors_by_threshold, sample_size, True, note,
    )


@dataclass(frozen=True)
class RoleRecommendation:
    rescued_titles: list[str]
    suggested_tokens: list[tuple[str, int]]  # (token/bigram, count), count >= 2
    confirmed_count: int
    note: str


_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _ngrams(text: str) -> set[str]:
    """Lowercase unigrams + adjacent bigrams of a title, deduped."""
    toks = _TOKEN_RE.findall(text.lower())
    grams = set(toks)
    grams.update(f"{a} {b}" for a, b in zip(toks, toks[1:]))
    return grams


def analyze_role_titles(
    rejected_rows: list[dict], *, current_titles: list[str]
) -> RoleRecommendation:
    rescued_titles: list[str] = []
    confirmed_count = 0
    for row in rejected_rows:
        if row.get("rejected_by") != "role":
            continue
        verdict = row.get("verdict")
        title = (row.get("title") or "").strip()
        if verdict == "rescued" and title:
            rescued_titles.append(title)
        elif verdict == "confirmed_rejected":
            confirmed_count += 1

    covered: set[str] = set()
    for phrase in current_titles:
        covered |= _ngrams(phrase)

    counter: Counter[str] = Counter()
    for title in rescued_titles:
        for gram in _ngrams(title):
            if gram not in covered:
                counter[gram] += 1
    suggested = [(g, n) for g, n in counter.most_common() if n >= 2]

    if not rescued_titles:
        note = "no role-rejected titles have been rescued yet — nothing to suggest"
    elif len(rescued_titles) < 3:
        note = (
            f"only {len(rescued_titles)} rescued title(s) — low confidence; each is a "
            f"concrete miss but suggested patterns are speculative"
        )
    else:
        note = (
            f"{len(rescued_titles)} rescued title(s) the role regex wrongly rejected; "
            f"tokens/bigrams in >= 2 of them are candidate additions to filters.titles"
        )
    return RoleRecommendation(rescued_titles, suggested, confirmed_count, note)


@dataclass(frozen=True)
class SourceScoreStats:
    source_family: str
    count: int
    median: float
    p25: float
    p75: float
    suppressed_rate: float  # fraction of scored rows never notified


def _family(job_id: str) -> str:
    return job_id.split(":", 1)[0]


def analyze_source_scores(rows: list[dict]) -> list[SourceScoreStats]:
    """Score distribution per source family (job_id prefix). Detects snippet-
    scored sources (adzuna) clustering abnormally vs full-JD ATS sources."""
    by_family: dict[str, list[dict]] = {}
    for row in rows:
        score = row.get("score")
        if not isinstance(score, int) or isinstance(score, bool):
            continue
        by_family.setdefault(_family(row.get("job_id", "")), []).append(row)
    out: list[SourceScoreStats] = []
    for fam in sorted(by_family):
        frows = by_family[fam]
        scores = sorted(r["score"] for r in frows)
        if len(scores) >= 2:
            q = statistics.quantiles(scores, n=4, method="inclusive")
            p25, med, p75 = float(q[0]), float(q[1]), float(q[2])
        else:
            p25 = med = p75 = float(scores[0])
        suppressed = sum(1 for r in frows if not r.get("notified"))
        out.append(SourceScoreStats(fam, len(scores), med, p25, p75, suppressed / len(scores)))
    return out


@dataclass(frozen=True)
class SnippetPair:
    adzuna_job_id: str
    other_job_id: str
    adzuna_score: int
    other_score: int
    delta: int  # other - adzuna; positive = full JD scored higher than the snippet


@dataclass(frozen=True)
class SnippetPairReport:
    pairs: list[SnippetPair]
    mean_delta: float | None
    note: str


def _pair_key(row: dict) -> tuple[str, str] | None:
    company = " ".join(_TOKEN_RE.findall((row.get("company") or "").lower()))
    title = " ".join(_TOKEN_RE.findall((row.get("title") or "").lower()))
    if not company or not title:
        return None
    return company, title


def _first_seen_dt(row: dict) -> datetime | None:
    try:
        return datetime.fromisoformat(row.get("first_seen") or "")
    except ValueError:
        return None


def analyze_snippet_pairs(rows: list[dict], *, window_days: int = 14) -> SnippetPairReport:
    """(normalized company+title) collisions between adzuna:* rows and
    direct-poll rows within a window — the same underlying job scored twice
    (snippet vs full JD). The delta distribution decides whether description
    enrichment is ever worth building (spec: measurement hooks)."""
    adzuna: dict[tuple[str, str], list[dict]] = {}
    others: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        score = row.get("score")
        if not isinstance(score, int) or isinstance(score, bool):
            continue
        key = _pair_key(row)
        if key is None:
            continue
        bucket = adzuna if _family(row.get("job_id", "")) == "adzuna" else others
        bucket.setdefault(key, []).append(row)
    pairs: list[SnippetPair] = []
    for key, arows in adzuna.items():
        for a in arows:
            a_seen = _first_seen_dt(a)
            for o in others.get(key, []):
                o_seen = _first_seen_dt(o)
                # Missing/unparseable first_seen skips the window filter (lenient by design).
                if a_seen and o_seen and abs(o_seen - a_seen) > timedelta(days=window_days):
                    continue
                pairs.append(SnippetPair(
                    a["job_id"], o["job_id"], a["score"], o["score"],
                    o["score"] - a["score"],
                ))
    mean_delta = (sum(p.delta for p in pairs) / len(pairs)) if pairs else None
    if not pairs:
        note = ("no adzuna/direct-poll pairs found yet — pairs appear when the "
                "conversion chain re-surfaces an adzuna-scored job via a direct poll")
    else:
        note = (f"{len(pairs)} pair(s); mean delta {mean_delta:+.1f} "
                "(positive = full-JD score higher than the adzuna snippet score)")
    return SnippetPairReport(pairs, mean_delta, note)
