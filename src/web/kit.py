"""Apply kit: recurring application-form facts, one tap from the clipboard.
The facts live in a gitignored YAML file on the box (kit.facts_path,
default resume/facts.yaml) — free-form label/value groups, hand-edited,
read per request so edits are save + refresh."""
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


def load_facts(path: str) -> list[FactGroup]:
    """Read and parse the facts file; FileNotFoundError when absent.
    Transitional — Task 11 switches /kit to the kit_facts document."""
    with open(path, encoding="utf-8") as fh:
        return parse_facts(fh.read())


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
        path = request.app.state.kit_facts_path
        groups: list[FactGroup] = []
        missing = False
        error = ""
        try:
            groups = load_facts(path)
        except FileNotFoundError:
            missing = True
        except FactsError as exc:
            error = str(exc)
        try:
            bookmarklet = build_bookmarklet(groups, _matcher_source()) if groups else ""
        except OSError:
            bookmarklet = ""
        return request.app.state.templates.TemplateResponse(
            request,
            "kit.html",
            {"groups": groups, "missing": missing, "error": error,
             "facts_path": path, "bookmarklet": bookmarklet},
        )
