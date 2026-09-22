"""Domain packages must not depend on the web layer."""
import pkgutil
import subprocess
import sys
from pathlib import Path

import pytest

import src.resume_intake
import src.settings

ROOT = Path(__file__).resolve().parents[2]


def _modules(package) -> list[str]:
    return sorted(
        f"{package.__name__}.{m.name}"
        for m in pkgutil.iter_modules(package.__path__)
        if m.name != "__main__"
    )


@pytest.mark.parametrize("module", _modules(src.resume_intake) + _modules(src.settings))
def test_module_pulls_in_no_part_of_the_web_layer(module):
    """src/resume_intake/draft.py used to import apply_patch and the section
    registry from src.web.settings, and that inversion once closed an import
    cycle through src/web/settings/__init__ (see src/resume_intake/errors.py).
    apply_patch now lives in src/settings/patch.py and the filter paths in
    src/settings/filters.py.

    Run in a fresh interpreter on purpose. In-process this would assert
    nothing: by the time the suite reaches it, hundreds of other tests have
    already put src.web.* in sys.modules, so the list would be non-empty
    however clean the module's own imports were."""
    code = (f"import sys, {module}; "
            "print(sorted(k for k in sys.modules if k.startswith('src.web')))")
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                         capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]"
