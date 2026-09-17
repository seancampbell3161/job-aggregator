"""Per-generation web objects: LLM engines and providers built from settings."""
from __future__ import annotations

import threading
from typing import Callable, TypeVar

from src.settings.service import ConfigSnapshot

T = TypeVar("T")


class GenerationCache:
    """Values built from a ConfigSnapshot, keyed by name and rebuilt when the
    snapshot's generation moves — expensive objects stay out of the request
    path without ever outliving the settings they were built from."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, tuple[int, object]] = {}

    def get(self, key: str, snap: ConfigSnapshot, build: Callable[[ConfigSnapshot], T]) -> T:
        with self._lock:
            hit = self._entries.get(key)
        if hit is not None and hit[0] == snap.generation:
            return hit[1]  # type: ignore[return-value]
        value = build(snap)  # outside the lock: builds may be slow
        with self._lock:
            current = self._entries.get(key)
            if current is None or current[0] <= snap.generation:
                self._entries[key] = (snap.generation, value)
        return value
