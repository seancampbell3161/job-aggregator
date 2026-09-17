"""Guard: the AWS deployment path is gone for good. No module in the app,
scripts, or tests may import the AWS SDKs or their mocks."""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BANNED = {"boto3", "botocore", "moto"}


def _imported_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def test_no_aws_sdk_imports():
    offenders = [
        f"{p.relative_to(ROOT)}: {sorted(_imported_roots(p) & BANNED)}"
        for d in ("src", "scripts", "tests")
        for p in (ROOT / d).rglob("*.py")
        if _imported_roots(p) & BANNED
    ]
    assert offenders == []


def test_no_aws_dependencies_declared():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "boto3" not in text and "moto" not in text


def test_no_infra_directory():
    assert not (ROOT / "infra").exists()
