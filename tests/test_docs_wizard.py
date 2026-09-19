"""The docs describe the path a newcomer actually takes now that /setup
offers a guided wizard (src/web/wizard/) ahead of the older start-from-
defaults / restore-backup / import-existing-files doors."""
import re
from pathlib import Path


def _section(text: str, heading: str) -> str:
    """The text of the section starting at `heading` (matched literally,
    case-insensitively) up to -- but NOT including -- the next `##`/`###`
    heading, or end of file if there isn't one.

    Slicing "from a heading to EOF" (as an earlier version of this file
    did) lets unrelated, later occurrences of the searched-for text satisfy
    an assertion that is only supposed to be about ONE section -- proven by
    fix round 1: deleting the CLI-import block from "Existing installs"
    left `test_existing_installs_section_still_documents_cli_import` green
    because two other, unrelated mentions of the same command exist further
    down the file. Bounding the slice to the next heading closes that.
    """
    match = re.search(re.escape(heading), text, re.IGNORECASE)
    assert match, f"heading not found: {heading!r}"
    rest = text[match.end():]
    next_heading = re.search(r"\n#{2,3} ", rest)
    return rest[: next_heading.start()] if next_heading else rest


def test_getting_started_leads_with_guided_setup():
    text = Path("GETTING_STARTED.md").read_text()
    assert "guided setup" in text.lower()


def test_getting_started_main_path_does_not_hand_author_config_yaml():
    """PRs #5-#9 moved settings into SQLite and made hand-authoring
    config.yaml unnecessary for a first install; this sub-project's wizard
    makes it actively the slow path. `cp config.example.yaml` must not
    appear in the newcomer-facing "Start it" / "Configure it" material —
    only in the existing-installs/expert material below it.

    NOTE ON THE BRIEF'S ORIGINAL VERSION OF THIS TEST: it sliced with
    `text[:text.find("## ", 200)]`. `find` matches the substring "## "
    wherever it occurs, including one character into a "### " (h3) heading
    (`#`+`##`+` `), and in this file "## Contents" (an h2) sits at ~offset
    372 -- before any config.yaml material regardless of edits -- so that
    slice was always just the opening blurb. It passed against the
    *original*, unfixed doc (verified: "cp config.example.yaml" was never
    in that ~370-char slice even before this commit), so it could never
    have caught the bug it claimed to guard against. This version locates
    the boundary by the actual section headings and checks ordering
    directly instead.
    """
    text = Path("GETTING_STARTED.md").read_text()
    lower = text.lower()
    start_idx = lower.index("## 1. start it")
    expert_idx = lower.index("### existing installs")
    assert start_idx < expert_idx
    main_path = lower[start_idx:expert_idx]
    assert "cp config.example.yaml" not in main_path


def test_guided_setup_is_introduced_before_hand_authoring_config_yaml():
    text = Path("GETTING_STARTED.md").read_text().lower()
    guided_idx = text.index("guided setup")
    config_idx = text.index("cp config.example.yaml")
    assert guided_idx < config_idx


def test_existing_installs_section_still_documents_cli_import():
    """Restructure, don't delete: self-hosters and existing installs still
    need the CLI import path; it must survive under the new heading --
    specifically IN that section, not merely somewhere later in the file
    (the doc also mentions `python -m src.settings import` in the Docker
    Compose upgrading section and the local dry-run section, which would
    satisfy an unbounded "rest of the file" check on their own).

    Matches the actual invocation (`settings import .` or `settings import
    /import`), not a bare substring -- this section also documents
    `settings import-env-secrets`, a different command whose name happens
    to start with the same characters ("import" immediately followed by a
    hyphen, not a space) and would satisfy a plain `in` check on its own.
    """
    text = Path("GETTING_STARTED.md").read_text()
    section = _section(text, "### Existing installs & config.yaml")
    assert re.search(r"settings import (\.|/import)\b", section)


def test_getting_started_matches_the_setup_page_cta():
    """/setup's guided-setup button (src/web/templates/setup.html) and the
    docs should name the same action, so a newcomer can find what the docs
    describe."""
    getting_started = Path("GETTING_STARTED.md").read_text()
    setup_page = Path("src/web/templates/setup.html").read_text()
    assert "Start guided setup" in setup_page
    assert "start guided setup" in getting_started.lower()


def test_config_md_has_a_row_per_resume_draft_flag():
    text = Path("docs/CONFIG.md").read_text()
    for flag in ("resume_draft.provider", "resume_draft.model", "resume_draft.timeout_seconds"):
        assert flag in text


def test_readme_feature_list_mentions_guided_setup():
    """Scoped to the '## What it does' section specifically -- an earlier
    version checked the whole file, which a later '## Configuration'
    mention of "guided setup wizard" would satisfy even if the feature-list
    bullet itself were deleted."""
    text = Path("README.md").read_text()
    section = _section(text, "## What it does")
    assert "guided setup" in section.lower()
