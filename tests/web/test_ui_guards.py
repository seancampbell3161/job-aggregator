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
