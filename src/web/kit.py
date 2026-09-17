"""Apply kit: recurring application-form facts, one tap from the clipboard.
The facts are the `kit_facts` settings document — free-form label/value
groups, imported with `python -m src.settings import` and read per request,
so a new import shows on the next refresh."""
from __future__ import annotations

from pathlib import Path

import json
import urllib.parse
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

from src.kit_facts import FactGroup, FactsError, parse_facts


_MATCHER_PATH = Path(__file__).parent / "static" / "bookmarklet.js"


def _matcher_source() -> str:
    return _MATCHER_PATH.read_text(encoding="utf-8")


def _facts_json(groups: list[FactGroup]) -> str:
    """Flat JSON array of {group,label,value}, safe to inline into a
    `javascript:` document. json.dumps \\u-escapes all non-ASCII (incl. the
    U+2028/U+2029 line separators that would otherwise terminate a JS string);
    we additionally escape `<` so a value containing `</script>` cannot break
    the host page's HTML parser."""
    flat = [{"group": g.name, "label": f.label, "value": f.value}
            for g in groups for f in g.facts]
    return json.dumps(flat).replace("<", "\\u003c")


def build_bookmarklet(groups: list[FactGroup], matcher_js: str) -> str:
    """The full `javascript:` bookmarklet: the matcher source inlined, then
    invoked with the baked-in facts, percent-encoded so the WHATWG URL parser
    (which strips raw tab/newline) cannot corrupt it — the body is restored
    exactly at click-time percent-decode."""
    body = (
        "(function(){"
        + matcher_js
        + ";__APPLY_FILL("
        + _facts_json(groups)
        + ");})()"
    )
    return "javascript:" + urllib.parse.quote(body)


def register_kit_routes(app: FastAPI) -> None:
    @app.get("/kit", response_class=HTMLResponse)
    def kit(request: Request):
        text = request.state.snapshot.documents.kit_facts
        groups: list[FactGroup] = []
        missing = text is None
        error = ""
        if not missing:
            try:
                groups = parse_facts(text)
            except FactsError as exc:
                error = str(exc)
        try:
            bookmarklet = build_bookmarklet(groups, _matcher_source()) if groups else ""
        except OSError:
            bookmarklet = ""
        return request.app.state.templates.TemplateResponse(
            request,
            "kit.html",
            {"groups": groups, "missing": missing, "error": error, "bookmarklet": bookmarklet},
        )
