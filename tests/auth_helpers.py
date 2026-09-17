"""Auth fixtures: a cheap argon2 hasher (more helpers are added by later tasks)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher


def cheap_hasher() -> PasswordHasher:
    """argon2id at minimal cost; the production defaults take ~35 ms per hash."""
    return PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)


class FakeClock:
    """A settable UTC clock for AuthService."""

    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now += timedelta(**kwargs)
