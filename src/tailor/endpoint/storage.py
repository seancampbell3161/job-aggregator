"""PDF storage for the tailor flow. run_tailor depends only on the PdfStorage
protocol; LocalFileStorage backs the web app."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol


class PdfStorage(Protocol):
    def exists(self, key: str) -> bool: ...
    def put(self, key: str, data: bytes, content_type: str) -> None: ...
    def get(self, key: str) -> bytes | None: ...
    def url(self, key: str, ttl: int) -> str: ...


class LocalFileStorage:
    """Writes PDFs under `root` and returns a path the web app serves at
    `base_url`. ttl is ignored — local files don't expire."""

    def __init__(self, root: str, base_url: str = "/tailored") -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._base_url = base_url.rstrip("/")

    def exists(self, key: str) -> bool:
        return (self._root / key).exists()

    def put(self, key: str, data: bytes, content_type: str) -> None:
        (self._root / key).write_bytes(data)

    def get(self, key: str) -> bytes | None:
        p = self._root / key
        return p.read_bytes() if p.exists() else None

    def url(self, key: str, ttl: int) -> str:
        return f"{self._base_url}/{key}"
