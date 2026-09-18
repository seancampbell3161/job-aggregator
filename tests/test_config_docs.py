"""Drift guard: every config flag has a row in docs/CONFIG.md, and every
documented dotted path still exists in the model tree. This is the mechanism
that keeps the reference honest — new flags fail CI until documented."""
import re
from pathlib import Path

from src.config import AppConfig
from src.settings.fields import all_paths

DOC_PATH = Path(__file__).resolve().parent.parent / "docs" / "CONFIG.md"


_DOTTED_IN_DOC = re.compile(r"`([a-z_]+(?:\.[a-z_0-9]+)+)`")


def test_every_config_flag_is_documented():
    paths = all_paths()
    # canary: the ForwardRef'd location gate must have been descended into
    assert "filters.location.allowed_countries" in paths
    doc = DOC_PATH.read_text()
    missing = sorted(p for p in paths if p not in doc)
    assert not missing, (
        "config flags missing from docs/CONFIG.md — add a table row for each:\n  "
        + "\n  ".join(missing)
    )


def test_no_stale_doc_rows():
    paths = all_paths()
    sections = set(AppConfig.model_fields)
    doc = DOC_PATH.read_text()
    documented = {
        m.group(1)
        for m in _DOTTED_IN_DOC.finditer(doc)
        if m.group(1).split(".")[0] in sections
    }
    stale = sorted(documented - paths)
    assert not stale, (
        "docs/CONFIG.md documents config paths that no longer exist "
        "(flag deleted or renamed?):\n  " + "\n  ".join(stale)
    )
