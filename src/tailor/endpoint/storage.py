"""Pluggable PDF storage for the tailor endpoint: S3 (hosted Lambda path) or
local files (the Docker Compose web app). run_tailor depends only on the
PdfStorage protocol below."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol


class PdfStorage(Protocol):
    def exists(self, key: str) -> bool: ...
    def put(self, key: str, data: bytes, content_type: str) -> None: ...
    def get(self, key: str) -> bytes | None: ...
    def url(self, key: str, ttl: int) -> str: ...


class S3Storage:
    """Preserves the hosted-endpoint behavior: objects live under key_prefix in
    the bucket; url() returns a presigned GET."""

    def __init__(self, s3: Any, bucket: str, key_prefix: str = "tailored/") -> None:
        self._s3 = s3
        self._bucket = bucket
        self._prefix = key_prefix

    def _k(self, key: str) -> str:
        return f"{self._prefix}{key}"

    def exists(self, key: str) -> bool:
        try:
            self._s3.head_object(Bucket=self._bucket, Key=self._k(key))
            return True
        except Exception:  # noqa: BLE001 — any error (404/access) -> absent
            return False

    def put(self, key: str, data: bytes, content_type: str) -> None:
        self._s3.put_object(Bucket=self._bucket, Key=self._k(key), Body=data,
                            ContentType=content_type)

    def get(self, key: str) -> bytes | None:
        try:
            return self._s3.get_object(Bucket=self._bucket, Key=self._k(key))["Body"].read()
        except Exception:  # noqa: BLE001 — absent/unreadable -> None
            return None

    def url(self, key: str, ttl: int) -> str:
        return self._s3.generate_presigned_url(
            "get_object", Params={"Bucket": self._bucket, "Key": self._k(key)}, ExpiresIn=ttl
        )


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
