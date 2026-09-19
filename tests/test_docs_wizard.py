"""The docs describe the path a newcomer actually takes now that /setup
offers a guided wizard (src/web/wizard/) ahead of the older start-from-
defaults / restore-backup / import-existing-files doors."""
from pathlib import Path


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
    need the CLI import path; it must survive under the new heading."""
    text = Path("GETTING_STARTED.md").read_text()
    lower = text.lower()
    expert_idx = lower.index("### existing installs")
    rest = text[expert_idx:]
    assert "python -m src.settings import" in rest


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
    text = Path("README.md").read_text().lower()
    assert "guided setup" in text
