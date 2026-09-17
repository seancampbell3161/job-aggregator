"""Settings-document schema migrations.

SCHEMA_VERSION is the version new rows are written at. MIGRATIONS[i] upgrades
a version-(i+1) document to version i+2, so len(MIGRATIONS) == SCHEMA_VERSION - 1.
Migrations run on read (every stored row) and before validation on save. A
migration takes and returns a plain dict and must not mutate its input.

To rename or reshape a key: bump SCHEMA_VERSION and append one function."""
from __future__ import annotations

from typing import Callable, Sequence

Migration = Callable[[dict], dict]

SCHEMA_VERSION = 1
MIGRATIONS: list[Migration] = []


def migrate(
    doc: dict, from_version: int, *,
    migrations: Sequence[Migration] = MIGRATIONS, to_version: int = SCHEMA_VERSION,
) -> dict:
    """Upgrade ``doc`` from ``from_version`` to ``to_version``. Raises
    ValueError for a version outside 1..to_version (for example a row written
    by a newer release)."""
    if len(migrations) != to_version - 1:
        raise ValueError(
            f"expected {to_version - 1} migrations for schema version {to_version}, "
            f"got {len(migrations)}"
        )
    if not 1 <= from_version <= to_version:
        raise ValueError(
            f"settings schema_version {from_version} is not supported "
            f"(this release reads 1..{to_version})"
        )
    for step in migrations[from_version - 1 : to_version - 1]:
        doc = step(doc)
    return doc
