"""Auth fixtures: a cheap argon2 hasher (more helpers are added by later tasks)."""
from __future__ import annotations

from argon2 import PasswordHasher


def cheap_hasher() -> PasswordHasher:
    """argon2id at minimal cost; the production defaults take ~35 ms per hash."""
    return PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
