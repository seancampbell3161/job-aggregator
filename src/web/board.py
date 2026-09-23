from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from src.state import VALID_STATUSES
from src.web.context import config_ctx
from src.web.repo import TriageMatch, TriageRepo

ACTIVE_COLUMNS: tuple[str, ...] = ("interested", "applied", "interviewing", "offer")
_PURSUED = frozenset({"interested", "applied", "interviewing", "offer", "rejected", "ghosted"})
# A hand-added job must land somewhere visible, so the add form offers only the
# statuses the board actually renders as a column.
ADDABLE_STATUSES: tuple[str, ...] = ACTIVE_COLUMNS


@dataclass
class Board:
    columns: dict[str, list[TriageMatch]]
    archive: list[TriageMatch]
    error: bool = False

    @property
    def is_empty(self) -> bool:
        """No card in any active column. A read error is not "empty" — the
        page says the data is unavailable instead."""
        return not self.error and not any(self.columns.values())


class BoardProvider:
    """Buckets pursued applications into kanban columns + an archive from a single
    TriageRepo scan. Fail-soft: a read error yields an empty board with error=True
    so the page renders instead of 500-ing."""

    def __init__(self, repo: TriageRepo) -> None:
        self._repo = repo

    def board(self) -> Board:
        empty = {c: [] for c in ACTIVE_COLUMNS}
        try:
            matches = self._repo.list()
        except Exception:  # noqa: BLE001 — page degrades, never 500s
            return Board(columns=empty, archive=[], error=True)
        columns: dict[str, list[TriageMatch]] = {c: [] for c in ACTIVE_COLUMNS}
        archive: list[TriageMatch] = []
        for m in matches:
            if m.status in columns:
                columns[m.status].append(m)
            elif m.status in ("rejected", "ghosted"):
                archive.append(m)
            elif m.status == "dismissed" and self._was_pursued(m):
                archive.append(m)
            # else: 'new', or an inbox-swipe 'dismissed' — excluded from the board
        for items in columns.values():
            items.sort(key=lambda m: m.days_in_stage, reverse=True)
        archive.sort(key=lambda m: m.current_since, reverse=True)
        return Board(columns=columns, archive=archive)

    @staticmethod
    def _was_pursued(m: TriageMatch) -> bool:
        return any(e.get("status") in _PURSUED for e in m.history)


def parse_comp(raw: str) -> int | None:
    """A comp box typed the way people actually type it — '180000', '$180,000',
    '180k' — as an int. Blank or unparseable means 'not given' (None) rather than
    an error: comp is optional garnish on a hand-added card, not worth a bounce."""
    s = raw.strip().lower().replace(",", "").replace("$", "").replace(" ", "")
    if not s:
        return None
    multiplier = 1
    if s.endswith("k"):
        s, multiplier = s[:-1], 1000
    try:
        value = int(float(s) * multiplier)
    except ValueError:
        return None
    return value if value > 0 else None


def _board_ctx(request: Request, **extra) -> dict:
    return {
        **config_ctx(request),
        "active_columns": ACTIVE_COLUMNS,
        "addable_statuses": ADDABLE_STATUSES,
        # Re-render state for the add form: empty on a plain GET, repopulated
        # from the submitted body when validation sends the user back.
        "add_error": "",
        "add_form": {},
        "add_open": False,
        **extra,
    }


def register_board_routes(app: FastAPI) -> None:
    @app.get("/board", response_class=HTMLResponse)
    def board(request: Request):
        b = request.app.state.board.board()
        add = request.query_params.get("add") == "1"
        return request.app.state.templates.TemplateResponse(
            request, "board.html", _board_ctx(request, board=b, add_open=add)
        )

    @app.post("/board/add")
    async def add_job(request: Request):
        """Add an opportunity that never came through a connector — a recruiter
        DM, a referral, a role someone mentioned. Plain form post + redirect (no
        htmx): a full-page GET after the write leaves the form empty and closed,
        and re-submitting on refresh is impossible. Reads the urlencoded body by
        hand for the same reason /bulk-status does — no python-multipart needed.

        On bad input it re-renders /board with the message and the typed values
        intact rather than throwing away what the user just wrote."""
        form = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
        values = {k: (v[0] if v else "") for k, v in form.items()}
        status = values.get("status", ADDABLE_STATUSES[0])

        def bounce(message: str):
            b = request.app.state.board.board()
            return request.app.state.templates.TemplateResponse(
                request, "board.html",
                _board_ctx(request, board=b, add_error=message, add_form=values),
                status_code=400,
            )

        if status not in ADDABLE_STATUSES:
            return bounce(f"Pick a stage: {', '.join(ADDABLE_STATUSES)}.")
        try:
            request.app.state.repo.add_manual(
                company=values.get("company", ""), title=values.get("title", ""),
                status=status, location_text=values.get("location_text", ""),
                apply_url=values.get("apply_url", ""),
                comp_min=parse_comp(values.get("comp_min", "")),
                comp_max=parse_comp(values.get("comp_max", "")),
                channel=values.get("channel", ""),
                description=values.get("description", ""),
            )
        except ValueError as exc:
            return bounce(str(exc).capitalize() + ".")
        return RedirectResponse("/board", status_code=303)

    @app.post("/board/advance", response_class=HTMLResponse)
    def advance(request: Request, id: str, status: str):
        if status not in VALID_STATUSES:
            raise HTTPException(status_code=400, detail=f"invalid status: {status}")
        request.app.state.repo.set_status(id, status)
        request.app.state.stores.seen.update_email_suggestion(id, suggestion=None)
        b = request.app.state.board.board()
        return request.app.state.templates.TemplateResponse(
            request, "_board_columns.html", _board_ctx(request, board=b)
        )

    @app.post("/board/dismiss-suggestion", response_class=HTMLResponse)
    def dismiss_suggestion(request: Request, id: str):
        request.app.state.stores.seen.update_email_suggestion(
            id, suggestion=None, record_dismissed=True
        )
        b = request.app.state.board.board()
        return request.app.state.templates.TemplateResponse(
            request, "_board_columns.html", _board_ctx(request, board=b)
        )
