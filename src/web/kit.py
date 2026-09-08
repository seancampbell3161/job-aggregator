"""Apply kit: recurring application-form facts, one tap from the clipboard.
The facts live in a gitignored YAML file on the box (kit.facts_path,
default resume/facts.yaml) — free-form label/value groups, hand-edited,
read per request so edits are save + refresh."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import json
import urllib.parse
import yaml
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse


_MATCHER_PATH = Path(__file__).parent / "static" / "bookmarklet.js"


def _matcher_source() -> str:
    return _MATCHER_PATH.read_text(encoding="utf-8")


class FactsError(Exception):
    """The facts file exists but its shape or YAML is wrong."""


@dataclass(frozen=True)
class Fact:
    label: str
    value: str


@dataclass(frozen=True)
class FactGroup:
    name: str
    facts: tuple[Fact, ...]


def _coerce(value: object) -> str:
    """Everything the template sees is a string. Unquoted YAML scalars are
    survived: booleans render Yes/No (a bare `No` parses as False), numbers
    via str(), null as empty string."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    return str(value)


def load_facts(path: str) -> list[FactGroup]:
    """Parse the facts file. Raises FileNotFoundError when the file is absent
    (the route renders a setup notice) and FactsError for YAML/shape problems
    (the route renders an error banner). Ordering is preserved as written."""
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise FactsError(f"not valid YAML: {exc}") from exc
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise FactsError("top level must be a list of groups (each `- group: ...`)")
    groups: list[FactGroup] = []
    for i, entry in enumerate(raw, start=1):
        if not isinstance(entry, dict) or not entry.get("group"):
            raise FactsError(f"entry {i}: missing a `group:` name")
        name = str(entry["group"])
        facts_raw = entry.get("facts")
        if facts_raw is None:
            facts_raw = []
        if not isinstance(facts_raw, list):
            raise FactsError(f"group '{name}': `facts:` must be a list")
        facts: list[Fact] = []
        for j, f in enumerate(facts_raw, start=1):
            if not isinstance(f, dict) or not f.get("label"):
                raise FactsError(f"group '{name}' fact {j}: missing a `label:`")
            if "value" not in f:
                raise FactsError(f"group '{name}' fact {j}: missing a `value:`")
            facts.append(Fact(label=str(f["label"]), value=_coerce(f["value"])))
        groups.append(FactGroup(name=name, facts=tuple(facts)))
    return groups


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
