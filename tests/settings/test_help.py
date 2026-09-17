"""Per-flag help parsed out of docs/CONFIG.md, the reference CI already guards."""
import pytest

from src.settings.fields import all_paths
from src.settings.help import field_help, group_intro, parse, render_inline

SAMPLE = """\
# Configuration Reference

## filters

Hard gates run on every posting.

| Flag | Default | What it does |
|---|---|---|
| `filters.titles` | `[]` | Title allowlist. A posting must match one entry. |
| `filters.comp_floor_usd` | `0` | Reject below this. Uses **minimum** comp, e.g. a range. `0` disables. |

## secrets

| Flag | Env var | What it does |
|---|---|---|
| `secrets.ntfy_topic_url` | `JOB_AGG_NTFY_TOPIC_URL` | ntfy topic. Empty = no pushes. |
"""


def test_parses_rows_into_help():
    helps, _ = parse(SAMPLE)
    assert set(helps) == {"filters.titles", "filters.comp_floor_usd", "secrets.ntfy_topic_url"}
    assert helps["filters.titles"].summary == "Title allowlist."
    assert "must match one entry" in helps["filters.titles"].full


def test_summary_does_not_split_on_an_abbreviation():
    helps, _ = parse(SAMPLE)
    summary = helps["filters.comp_floor_usd"].summary
    assert summary == "Reject below this."


def test_group_intro_is_the_prose_under_the_heading():
    _, intros = parse(SAMPLE)
    assert intros["filters"] == "Hard gates run on every posting."
    assert "secrets" in intros


def test_separator_and_header_rows_are_ignored():
    helps, _ = parse(SAMPLE)
    assert not any(p.startswith("-") or p == "Flag" for p in helps)


def test_a_description_containing_a_pipe_survives():
    helps, _ = parse(
        "## x\n\n| Flag | Default | What |\n|---|---|---|\n"
        "| `x.y` | `1` | Either `a` | `b` here. |\n"
    )
    assert "`b`" not in helps["x.y"].full  # rendered to <code>
    assert "b</code> here." in helps["x.y"].full


def test_render_inline_escapes_then_formats():
    out = render_inline("Use `a<b>` and **bold** and [docs](../X.md).")
    assert "<code>a&lt;b&gt;</code>" in out
    assert "<strong>bold</strong>" in out
    assert '<a href="../X.md">docs</a>' in out
    assert "<b>" not in out


def test_missing_file_is_soft(monkeypatch, tmp_path):
    import src.settings.help as help_mod
    monkeypatch.setattr(help_mod, "_DOC_PATH", tmp_path / "nope.md")
    help_mod._load.cache_clear()
    assert field_help("filters.titles") is None
    help_mod._load.cache_clear()


@pytest.mark.parametrize("path", sorted(all_paths()))
def test_every_config_flag_resolves_help(path):
    """The companion to the docs drift guard: the rows exist (that test) AND we
    can read them (this one)."""
    assert field_help(path) is not None, f"{path} has a CONFIG.md row the parser cannot read"


def test_group_intro_for_a_real_section():
    assert group_intro("discovery")


def test_real_docs_render_with_no_residual_markdown():
    """Sweep the real docs/CONFIG.md: every rendered help string and group
    intro must be free of a stray `**` or an unbalanced backtick. The
    per-path non-nullness test can't catch this — it only proves a row was
    found, not that its rendering is clean — so this is the guard for
    parser bugs like an intro that swallows a trailing fenced example, or a
    bold span that can't cross an embedded code span."""
    for path in sorted(all_paths()):
        help_ = field_help(path)
        assert help_ is not None
        for rendered in (help_.summary, help_.full):
            assert "**" not in rendered, f"{path}: unrendered bold — {rendered!r}"
            assert rendered.count("`") % 2 == 0, f"{path}: unbalanced backtick — {rendered!r}"

    import src.settings.help as help_mod
    _, intros = help_mod._load()
    assert intros, "the real file should have parsed at least one section intro"
    for section, intro in intros.items():
        assert "**" not in intro, f"{section} intro: unrendered bold — {intro!r}"
        assert intro.count("`") % 2 == 0, f"{section} intro: unbalanced backtick — {intro!r}"
