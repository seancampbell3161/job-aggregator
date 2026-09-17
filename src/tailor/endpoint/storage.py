"""PDF storage for the tailor flow. run_tailor depends only on the PdfStorage
protocol; LocalFileStorage backs the web app, which serves the files back
through the token-checked /tailor/pdf route."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol


def pdf_key(job_id: str, template: str = "") -> str:
    """A tailored PDF's file name: `<job_id>.pdf` for the tailor run itself,
    `<job_id>.<template>.pdf` for a re-render through another template pack.
    run_tailor writes under this name and /tailor/pdf reads it back."""
    return f"{job_id}.{template}.pdf" if template else f"{job_id}.pdf"


class PdfStorage(Protocol):
    def exists(self, key: str) -> bool: ...
    def put(self, key: str, data: bytes, content_type: str) -> None: ...
    def get(self, key: str) -> bytes | None: ...


class LocalFileStorage:
    """Files under `root`."""

    def __init__(self, root: str) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    def exists(self, key: str) -> bool:
        return (self._root / key).exists()

    def put(self, key: str, data: bytes, content_type: str) -> None:
        (self._root / key).write_bytes(data)

    def get(self, key: str) -> bytes | None:
        p = self._root / key
        return p.read_bytes() if p.exists() else None
