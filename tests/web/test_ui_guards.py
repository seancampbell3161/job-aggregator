"""Guards that keep the UI built from one design system (spec:
docs/superpowers/specs/2026-09-23-ui-foundation-design.md). Each guard
fails the build on the specific drift it names, the same way the CONFIG.md
drift guard does for config flags."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CSS_DIR = ROOT / "src/web/static/css"
TEMPLATES = ROOT / "src/web/templates"

_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_BODY = re.compile(r"\{([^{}]*)\}")
_COLOUR = re.compile(
    r"#[0-9a-fA-F]{3,8}\b|\brgba?\(|\bhsla?\(|"
    # (?<![\w-]) / (?![\w-]): a colour keyword, not part of `white-space`
    r"(?<![\w-])(?:white|black|red|green|blue|gray|grey|orange|yellow|purple)(?![\w-])")


def _declarations(css: str) -> list[str]:
    """Every declaration block's body, comments removed. Colours can only
    appear in declarations; scanning bodies (not selectors) keeps ID
    selectors like #detail or #fade from reading as hex colours."""
    return _BODY.findall(_COMMENT.sub("", css))


def test_no_colour_literal_outside_tokens():
    offenders = []
    for path in sorted(CSS_DIR.rglob("*.css")):
        if path.name == "tokens.css":
            continue
        for body in _declarations(path.read_text()):
            for m in _COLOUR.finditer(body):
                offenders.append(f"{path.relative_to(ROOT)}: {m.group(0)!r} in {{{body.strip()[:60]}…}}")
    assert not offenders, "colour literal outside tokens.css — add a token:\n" + "\n".join(offenders)


def test_tokens_file_exists():
    assert (CSS_DIR / "tokens.css").is_file()


STATIC = ROOT / "src/web/static"
PY_SOURCES = ROOT / "src/web"

_CLASS_ATTR = re.compile(r'\bclass="([^"]*)"')
_EXPR = re.compile(r"\{\{(.*?)\}\}", re.S)
_TAG = re.compile(r"\{%.*?%\}", re.S)
_LITERAL = re.compile(r"(==|!=|\bin)?\s*'([^']*)'")
_SENTINEL = re.compile(r"\x00(\d+)\x00")

# Class families whose members are chosen at runtime (s-{{ band }},
# st-{{ status }}, pl-{{ id }}, sev-{{ level }}). The template side can't
# name them; the stylesheet side is allowed to define them.
DYNAMIC_PREFIXES = ("s-", "st-", "pl-", "sev-")

# Classes a template uses that no stylesheet needs to define — each is a hook
# for something other than our CSS. Keep the reason next to every entry.
UNSTYLED_HOOKS = {
    "htmx-indicator": "shown/hidden by htmx's own injected stylesheet",
    "recipe-btn": "wizard_llm.html script binds its click handler by class",
    "chip-set": "base.html click delegation finds variant-set buttons by class",
}

# Classes that reach the page as macro arguments rather than in a class=""
# attribute: ui.alert('<kind>', …) and extra_class='<classes>'.
_MACRO_CLASSES = re.compile(r"""ui\.alert\(\s*'([\w-]+)'|extra_class\s*=\s*'([\w -]+)'""")

# Classes our CSS defines that no template or .py file names statically.
JS_ONLY = {
    "is-busy": "added and removed by the submit handler in base.html",
}


def _literals(expr: str) -> list[str]:
    """The string literals an expression can emit — not the ones it compares
    against (`s.state == 'paused'` names a state, not a class)."""
    return [lit for op, lit in _LITERAL.findall(expr) if not op]


def template_classes(attr: str) -> set[str]:
    """Class names a `class="…"` attribute value can produce.

    Static tokens count as-is. A standalone `{{ … }}` contributes its
    literals. A token that glues static text to an expression is a dynamic
    family (`s-{{ band }}`) and contributes nothing — unless every literal
    the expression emits starts with a space (`bad{{ ' dim' if … }}`), in
    which case the static part and the literals are separate classes."""
    exprs: list[str] = []

    def stash(m):
        exprs.append(m.group(1))
        return f"\x00{len(exprs) - 1}\x00"

    text = _EXPR.sub(stash, _TAG.sub(" ", attr))
    out: set[str] = set()
    for token in text.split():
        parts = _SENTINEL.split(token)
        statics, idxs = parts[0::2], [int(i) for i in parts[1::2]]
        lits = [lit for i in idxs for lit in _literals(exprs[i])]
        glued = idxs and any(statics)
        if glued and (not lits or any(lit and not lit[0].isspace() for lit in lits)):
            continue
        out.update(s for s in statics if s)
        for lit in lits:
            out.update(lit.split())
    return out


def _used_classes(paths) -> dict[str, set[str]]:
    used: dict[str, set[str]] = {}
    for path in paths:
        text = path.read_text()
        for attr in _CLASS_ATTR.findall(text):
            for cls in template_classes(attr):
                used.setdefault(cls, set()).add(path.name)
        for kind, extra in _MACRO_CLASSES.findall(text):
            for cls in (kind + " " + extra).split():
                used.setdefault(cls, set()).add(path.name)
    return used


def _templates():
    return sorted(TEMPLATES.glob("*.html"))


def _py_sources():
    return sorted(PY_SOURCES.rglob("*.py"))


def _css_classes(paths) -> set[str]:
    found: set[str] = set()
    for path in paths:
        css = _COMMENT.sub("", path.read_text())
        selectors = _BODY.sub("{}", css)   # drop declarations; keep selectors and @-rules
        found.update(re.findall(r"\.(-?[A-Za-z_][\w-]*)", selectors))
    return found


def _all_css():
    return sorted(CSS_DIR.rglob("*.css"))


# ---- extractor unit tests ----

def test_extractor_static_and_standalone_expressions():
    assert template_classes("field {{ 'has-error' if e }}{{ ' is-required' if r }}") == {
        "field", "has-error", "is-required"}


def test_extractor_skips_compared_literals():
    attr = "{{ 'ok' if s.state == 'healthy' else 'warn' if s.state == 'paused' else 'bad' }}"
    assert template_classes(attr) == {"ok", "warn", "bad"}


def test_extractor_skips_dynamic_families():
    assert template_classes("score s-{{ m.band(h, l) }}") == {"score"}
    assert template_classes("fam-row sev-{{ 'hi' if n else 'lo' }}") == {"fam-row"}


def test_extractor_splits_space_prefixed_glued_literals():
    assert template_classes("bad{{ ' dim' if old else '' }}") == {"bad", "dim"}


def test_extractor_reads_through_statement_tags():
    assert template_classes("{% if on %}on{% endif %}") == {"on"}


def test_macro_argument_classes_count_as_used(tmp_path):
    page = tmp_path / "p.html"
    page.write_text("{{ ui.alert('info', m) }}{{ ui.card(extra_class='danger-edge x') }}")
    assert set(_used_classes([page])) == {"info", "danger-edge", "x"}


# ---- guards ----

def test_app_css_is_gone():
    assert not (STATIC / "app.css").exists(), "static/app.css is legacy — its rules belong in static/css/"


def test_no_static_inline_style():
    """A style="" attribute is only allowed when its value is data-driven
    (a bar height, a flex-grow proportion); everything else is a class."""
    offenders = []
    for path in _templates():
        for m in re.finditer(r'\bstyle="([^"]*)"', path.read_text()):
            if "{{" not in m.group(1):
                offenders.append(f"{path.name}: style=\"{m.group(1)}\"")
    assert not offenders, "static inline style — use a class:\n" + "\n".join(offenders)


def test_no_style_block_in_templates():
    offenders = [p.name for p in _templates() if re.search(r"<style\b", p.read_text())]
    assert not offenders, f"<style> block in {offenders} — move it to static/css/pages/"


def test_every_button_is_a_styled_button():
    offenders = []
    for path in _templates():
        for m in re.finditer(r"<button\b[^>]*>", path.read_text()):
            classes = _CLASS_ATTR.search(m.group(0))
            if not classes or "btn" not in classes.group(1).split():
                offenders.append(f"{path.name}: {m.group(0)[:80]}")
    assert not offenders, "<button> without the btn class:\n" + "\n".join(offenders)


def test_templates_using_ui_import_it():
    offenders = [p.name for p in _templates()
                 if p.name != "_ui.html"
                 and re.search(r"\bui\.\w+\(", p.read_text())
                 and '{% import "_ui.html" as ui %}' not in p.read_text()]
    assert not offenders, f"uses ui.* without importing _ui.html: {offenders}"


def test_every_template_class_is_styled():
    defined = _css_classes(_all_css())
    used = _used_classes(_templates() + _py_sources())
    missing = sorted(f"{cls} ({', '.join(sorted(files))})"
                     for cls, files in used.items()
                     if cls not in defined and cls not in UNSTYLED_HOOKS)
    assert not missing, ("class used but styled nowhere — add a rule, delete the class, "
                         "or list it in UNSTYLED_HOOKS with the reason:\n" + "\n".join(missing))


def test_every_css_class_is_used():
    used = set(_used_classes(_templates() + _py_sources()))
    dead = sorted(cls for cls in _css_classes(_all_css())
                  if cls not in used and cls not in JS_ONLY
                  and not cls.startswith(DYNAMIC_PREFIXES))
    assert not dead, "CSS class no template uses — delete the rule:\n" + "\n".join(dead)


# Partials that htmx swaps into pages of more than one area, so they can't
# rely on any one page's stylesheet.
SHARED_PARTIALS = ("_company_probe.html", "_probe_result.html", "_settings_macros.html", "_ui.html")


def test_shared_partials_use_only_component_classes():
    shared_css = [CSS_DIR / "base.css", CSS_DIR / "components.css"]
    defined = _css_classes(shared_css)
    used = _used_classes([TEMPLATES / name for name in SHARED_PARTIALS])
    missing = sorted(f"{cls} ({', '.join(sorted(f))})" for cls, f in used.items()
                     if cls not in defined and cls not in UNSTYLED_HOOKS)
    assert not missing, ("shared partial uses a page-only class — move the rule to "
                         "components.css:\n" + "\n".join(missing))
