"""Coach: on-demand LLM recommendations for improving application response
rates, grounded in the user's own funnel/audit/config/résumé data. Run history
reports unavailable when no run store is wired and the page says so instead
of 500-ing."""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse

from src.coach import CoachSnapshot, build_snapshot

log = logging.getLogger(__name__)


def _config_block(cfg) -> dict:
    """The filter/relevance knob values the LLM may advise on. Values only —
    never secrets."""
    return {
        "filters": {
            "titles": list(cfg.filters.titles),
            "seniority_allow": list(cfg.filters.seniority_allow),
            "comp_floor_usd": cfg.filters.comp_floor_usd,
            "stack_any_of": list(cfg.filters.stack_any_of),
            "max_age_days": cfg.filters.max_age_days,
            "location": {
                "allowed_countries": list(cfg.filters.location.allowed_countries),
                "allowed_cities": list(cfg.filters.location.allowed_cities),
                "remote_policy": cfg.filters.location.remote_policy,
                "allow_unknown": cfg.filters.location.allow_unknown,
            },
        },
        "relevance": {
            "score_high": cfg.relevance.score_high,
            "score_low": cfg.relevance.score_low,
        },
    }


class CoachProvider:
    """Fail-soft accessors in the AuditProvider mold: readers return their
    result or None/[] on any error. run() is the only writer; it never raises
    (the engine is fail-open, and persistence failures degrade to None)."""

    def __init__(
        self, *, store=None, seen=None, rejected=None, engine=None, cfg=None,
        provider_name: str = "", model_name: str = "", enabled: bool = True,
    ) -> None:
        self._store = store
        self._seen = seen
        self._rejected = rejected
        self._engine = engine
        self._cfg = cfg
        self._provider_name = provider_name
        self._model_name = model_name
        self._enabled = enabled
        self._inflight = False

    @property
    def available(self) -> bool:
        return self._store is not None

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def can_run(self) -> bool:
        return self.available and self._engine is not None

    @property
    def inflight(self) -> bool:
        return self._inflight

    @property
    def nav_visible(self) -> bool:
        return self._enabled and self.available

    def latest(self) -> dict | None:
        if not self.available:
            return None
        try:
            return self._store.latest()
        except Exception as exc:  # noqa: BLE001 — panel degrades, page survives
            log.warning("coach_latest_unavailable", extra={"error": str(exc)})
            return None

    def runs(self, *, limit: int = 20) -> list[dict]:
        if not self.available:
            return []
        try:
            return self._store.list_runs(limit=limit)
        except Exception as exc:  # noqa: BLE001
            log.warning("coach_runs_unavailable", extra={"error": str(exc)})
            return []

    def get(self, run_id: str) -> dict | None:
        if not self.available:
            return None
        try:
            return self._store.get(run_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("coach_get_unavailable", extra={"error": str(exc)})
            return None

    def _audit_block(self, since_iso: str, window_days: int) -> dict | None:
        if self._rejected is None or self._seen is None:
            return None
        try:
            suppressed = self._seen.list_suppressed_details(since_iso=since_iso)
            rescued = self._seen.list_rescued_suppressions(since_iso=since_iso)
            confirmed = sum(
                1 for it in suppressed if it.get("audit_verdict") == "confirmed_rejected"
            )
            return {
                "window_days": window_days,
                "rejected_by_gate": dict(self._rejected.counts_by_gate(since_iso=since_iso)),
                "score_low_suppressed": len(suppressed),
                "rescued_from_suppression": len(rescued),
                "confirmed_rejected": confirmed,
            }
        except Exception as exc:  # noqa: BLE001 — snapshot degrades, run proceeds
            log.warning("coach_audit_block_unavailable", extra={"error": str(exc)})
            return None

    def build_run_snapshot(self, *, now: datetime | None = None) -> CoachSnapshot:
        """Assemble the snapshot from stores + config. Every input degrades
        independently: a missing profile/content file or a dead audit store
        shrinks the snapshot instead of blocking the run."""
        now = now or datetime.now(timezone.utc)
        matches = self._seen.list_matches() if self._seen is not None else []
        cfg = self._cfg
        config = profile_text = content = None
        if cfg is not None:
            config = _config_block(cfg)
            try:
                profile_text = Path(cfg.relevance.profile_path).read_text()
            except (OSError, ValueError):
                log.warning("coach_profile_unreadable", extra={"path": cfg.relevance.profile_path})
            try:
                from src.tailor.content import load_content
                content = load_content(cfg.tailoring.content_path)
            except (OSError, ValueError, json.JSONDecodeError):
                log.warning("coach_content_unreadable", extra={"path": cfg.tailoring.content_path})
        window_days = cfg.coach.window_days if cfg is not None else 90
        max_jobs = cfg.coach.max_jobs if cfg is not None else 100
        since_iso = (now - timedelta(days=window_days)).isoformat()
        return build_snapshot(
            matches=matches, config=config, profile_text=profile_text, content=content,
            audit=self._audit_block(since_iso, window_days), max_jobs=max_jobs, now=now,
        )

    async def run(self) -> dict | None:
        """Build a snapshot, call the LLM, persist the run (including error
        runs — a failed LLM call is a visible outcome, not a silent no-op).
        Returns the persisted run dict, or None when the provider can't run,
        a run is already in flight, or persistence itself failed."""
        if not self.can_run or self._inflight:
            return None
        self._inflight = True
        try:
            snapshot = self.build_run_snapshot()
            result = await self._engine.recommend(snapshot)
            run_id = uuid.uuid4().hex
            self._store.record(
                run_id=run_id,
                provider=self._provider_name, model=self._model_name,
                status="error" if result.is_fallback else "ok",
                snapshot_json=snapshot.to_json(),
                result_json=json.dumps([asdict(c) for c in result.cards]),
                error=result.error_type,
            )
            return self._store.get(run_id)
        except Exception as exc:  # noqa: BLE001 — the page must render, never 500
            log.warning("coach_run_failed", extra={"error": str(exc)})
            return None
        finally:
            self._inflight = False


def register_coach_routes(app: FastAPI) -> None:
    @app.get("/coach", response_class=HTMLResponse)
    def coach(request: Request):
        prov = request.app.state.coach
        return request.app.state.templates.TemplateResponse(
            request, "coach.html",
            {"available": prov.available, "enabled": prov.enabled,
             "can_run": prov.can_run, "run": prov.latest(),
             "runs": prov.runs(), "viewing_past": False},
        )

    @app.get("/coach/runs/{run_id}", response_class=HTMLResponse)
    def coach_past_run(request: Request, run_id: str):
        prov = request.app.state.coach
        run = prov.get(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="unknown run")
        return request.app.state.templates.TemplateResponse(
            request, "coach.html",
            {"available": prov.available, "enabled": prov.enabled,
             "can_run": prov.can_run, "run": run,
             "runs": prov.runs(), "viewing_past": True},
        )

    @app.post("/coach/run", response_class=HTMLResponse)
    async def coach_run(request: Request):
        prov = request.app.state.coach
        if not prov.can_run:
            raise HTTPException(status_code=409, detail="coach engine not configured")
        busy = prov.inflight
        if not busy:
            # Synchronous await, the /audit/rescue pattern: the htmx request
            # holds until the LLM answers (30-60s on a large local model).
            await prov.run()
        return request.app.state.templates.TemplateResponse(
            request, "_coach_run.html", {"run": prov.latest(), "busy": busy},
        )
