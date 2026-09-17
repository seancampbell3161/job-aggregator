"""Audit view: everything the pipeline rejected, with reasons and actions.
Read-time union of rejected_postings (filter gates) and score-low suppressed
seen_jobs rows. The provider fails soft: if a lookup errors, it reports
unavailable and the page says so instead of 500-ing."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from src.state import posting_from_item
from src.web.context import config_ctx

log = logging.getLogger(__name__)

_GATES = ("age", "role", "seniority", "location", "stack", "comp", "score_low")


@dataclass(frozen=True)
class AuditRow:
    job_id: str
    title: str
    company: str
    source: str
    location_text: str
    first_seen: str
    gate: str            # rejecting gate, or "score_low"
    reason: str
    apply_url: str
    verdict: str | None
    can_rescue: bool

    @property
    def apply_href(self) -> str:
        """http(s)-only guard, same defense as TriageMatch.apply_href."""
        url = self.apply_url or ""
        return url if url.startswith(("https://", "http://")) else "#"

    @property
    def date_display(self) -> str:
        return (self.first_seen or "")[:10]


def _from_rejected(item: dict) -> AuditRow:
    gate = item.get("rejected_by", "unknown")
    return AuditRow(
        job_id=item["job_id"], title=item.get("title", ""),
        company=item.get("company", ""), source=item.get("source", ""),
        location_text=item.get("location_text", ""),
        first_seen=item.get("first_seen", ""), gate=gate,
        reason=f"filter: {gate}", apply_url=item.get("apply_url", ""),
        verdict=item.get("verdict"), can_rescue=bool(item.get("title")),
    )


def _from_suppressed(item: dict) -> AuditRow:
    rationale = item.get("rationale") or "no rationale recorded"
    return AuditRow(
        job_id=item["job_id"], title=item.get("title", ""),
        company=item.get("company", ""), source=item.get("source", ""),
        location_text=item.get("location_text", ""),
        first_seen=item.get("first_seen", ""), gate="score_low",
        reason=f"score {item.get('score')}/10 — {rationale}",
        apply_url=item.get("apply_url", ""),
        verdict=item.get("audit_verdict"), can_rescue=bool(item.get("title")),
    )


class AuditProvider:
    """Fail-soft accessors in the OpsProvider mold: each returns its result or
    None on any error so the page renders 'unavailable' instead of 500-ing."""

    def __init__(self, *, rejected=None, seen=None) -> None:
        self._rejected = rejected
        self._seen = seen

    @property
    def available(self) -> bool:
        return self._rejected is not None

    @staticmethod
    def _since(days: int, now: datetime | None) -> str:
        return ((now or datetime.now(timezone.utc)) - timedelta(days=days)).isoformat()

    def tally(self, *, days: int = 7, now: datetime | None = None) -> dict | None:
        if not self.available:
            return None
        try:
            since = self._since(days, now)
            counts = dict(self._rejected.counts_by_gate(since_iso=since))
            suppressed = len(self._seen.list_suppressed_details(since_iso=since))
            if suppressed:
                counts["score_low"] = suppressed
            return {
                "total": sum(counts.values()),
                "by_gate": sorted(counts.items(), key=lambda kv: -kv[1]),
                "window_days": days,
            }
        except Exception as exc:  # noqa: BLE001 — panel degrades, page survives
            log.warning("audit_tally_unavailable", extra={"error": str(exc)})
            return None

    def rows(
        self, *, days: int = 7, gate: str = "", q: str = "",
        show_judged: bool = False, now: datetime | None = None,
    ) -> list[AuditRow] | None:
        if not self.available:
            return None
        try:
            since = self._since(days, now)
            out: list[AuditRow] = []
            if gate != "score_low":
                out.extend(
                    _from_rejected(it) for it in self._rejected.list_rejected(
                        since_iso=since, gate=gate or None, query=q,
                        include_judged=True,
                    )
                )
            if gate in ("", "score_low"):
                ql = q.lower()
                for it in self._seen.list_suppressed_details(since_iso=since):
                    hay = f"{it.get('title', '')} {it.get('company', '')} {it.get('source', '')}".lower()
                    if ql and ql not in hay:
                        continue
                    out.append(_from_suppressed(it))
            # A job_id can legitimately land in both sources (filter-rejected,
            # then re-fetched, passed the widened filter, and score-suppressed).
            # Dedupe by job_id, preferring the suppressed row — it carries the
            # score/rationale, which is more useful than the stale filter gate.
            by_id: dict[str, AuditRow] = {}
            for r in out:
                if r.job_id not in by_id or r.gate == "score_low":
                    by_id[r.job_id] = r
            out = list(by_id.values())
            if not show_judged:
                out = [r for r in out if r.verdict is None]
            out.sort(key=lambda r: r.first_seen, reverse=True)
            return out
        except Exception as exc:  # noqa: BLE001
            log.warning("audit_rows_unavailable", extra={"error": str(exc)})
            return None


def audit_llm(request: Request) -> tuple[object | None, object | None]:
    """(relevance scorer, gap analyzer) for rescues, built from the request's
    snapshot and cached per settings generation. Fail-soft: a broken
    client/API key degrades to (None, None) — the rescue route already
    tolerates that — instead of raising on every request until the
    generation moves."""
    def build(snap):
        from src.handler import _build_gap_analyzer, _build_relevance_scorer
        try:
            return (
                _build_relevance_scorer(snap.cfg, snap.documents.profile),
                _build_gap_analyzer(snap.cfg, snap.documents.resume_text),
            )
        except Exception as exc:  # noqa: BLE001 — rescue must not 500
            log.warning("audit_llm_build_failed", extra={"error": str(exc)})
            return (None, None)
    return request.app.state.cache.get("audit_llm", request.state.snapshot, build)


def register_audit_routes(app: FastAPI) -> None:
    @app.get("/audit", response_class=HTMLResponse)
    def audit(request: Request, gate: str = "", q: str = "", days: int = 7,
              judged: bool = False):
        prov = request.app.state.audit
        return request.app.state.templates.TemplateResponse(
            request, "audit.html",
            {
                **config_ctx(request),
                "available": prov.available,
                "tally": prov.tally(days=days),
                "rows": prov.rows(days=days, gate=gate, q=q, show_judged=judged),
                "gate": gate, "q": q, "days": days, "judged": judged,
                "gates": _GATES,
            },
        )

    @app.post("/audit/rescue")
    async def rescue(request: Request, id: str):
        stores = request.app.state.stores
        rejected = stores.rejected
        item = rejected.get(id)
        origin = "filter" if item is not None else "score_low"
        if item is None:
            item = stores.seen.get_suppressed(id)
        if item is None or not item.get("title"):
            raise HTTPException(status_code=404, detail="unknown or unrescuable job_id")
        # Capture the ORIGINAL suppressed score before the transition below
        # deletes the suppressed row — item is the get_suppressed(id) result
        # only when origin is score_low (the filter-origin item has no score).
        suppressed_score = item.get("score") if origin == "score_low" else None
        posting = posting_from_item(item)
        scorer, analyzer = audit_llm(request)
        score_val = rationale = None
        gaps = None
        if scorer is not None and posting.description:
            try:
                s = await scorer.score(posting)
                score_val, rationale = s.value, s.rationale
            except Exception as exc:  # noqa: BLE001 — rescue must not dead-end on LLM failure
                log.warning("audit_rescue_score_failed", extra={"job_id": id, "error": str(exc)})
        if analyzer is not None and posting.description:
            try:
                g = await analyzer.analyze(posting)
                gaps = g.skills or None
            except Exception as exc:  # noqa: BLE001
                log.warning("audit_rescue_gaps_failed", extra={"job_id": id, "error": str(exc)})
        if stores.seen.get_suppressed(id) is not None:
            # A suppressed row may exist even for filter-origin rescues (the
            # posting can be re-fetched, pass widened filters, then get
            # score-suppressed). Drop it so the claim below writes fresh —
            # claim_for_notify is INSERT OR IGNORE and would otherwise no-op
            # against the existing notified=0 row.
            stores.seen.release_claim(id)
        stores.seen.claim_for_notify(
            id, score=score_val, rationale=rationale, gaps=gaps, posting=posting
        )
        if origin == "filter":
            rejected.set_verdict(id, "rescued")
        if suppressed_score is not None:
            # score_low-origin rescue: record the durable rescue signal
            # (verdict + original score) so the tuning CLI can see it — the
            # suppressed row itself is already gone (released above).
            stores.seen.mark_rescued_from_suppression(id, suppressed_score=suppressed_score)
        return RedirectResponse(url="/audit", status_code=303)

    @app.post("/audit/confirm")
    def confirm(request: Request, id: str):
        stores = request.app.state.stores
        done_rejected = bool(stores.rejected.set_verdict(id, "confirmed_rejected"))
        done_suppressed = bool(stores.seen.set_audit_verdict(id, "confirmed_rejected"))
        if not (done_rejected or done_suppressed):
            raise HTTPException(status_code=404, detail="unknown job_id")
        return RedirectResponse(url="/audit", status_code=303)
