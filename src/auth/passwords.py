"""Password hashing (argon2id via argon2-cffi) and the new-password rule.

Each stored hash string embeds its own parameters, so verify() keeps working
after the parameters change and needs_rehash() reports hashes made with older
ones."""
from __future__ import annotations

import logging

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from src.auth.errors import PasswordRejected

log = logging.getLogger(__name__)

MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 1024


def validate_new_password(password: str) -> None:
    """Length only — no composition rules. Counted in characters, not bytes."""
    if len(password) < MIN_PASSWORD_LENGTH:
        raise PasswordRejected(f"Use at least {MIN_PASSWORD_LENGTH} characters.")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise PasswordRejected(f"Use at most {MAX_PASSWORD_LENGTH} characters.")


class Passwords:
    def __init__(self, hasher: PasswordHasher | None = None) -> None:
        # The library defaults are the RFC 9106 low-memory profile; tests
        # inject a cheap hasher.
        self._hasher = hasher if hasher is not None else PasswordHasher()

    def hash(self, password: str) -> str:
        return self._hasher.hash(password)

    def verify(self, stored_hash: str, password: str) -> bool:
        try:
            return self._hasher.verify(stored_hash, password)
        except VerifyMismatchError:
            return False
        except (VerificationError, InvalidHashError) as exc:
            # A corrupt stored hash: nobody can sign in until
            # `python -m src.settings set-password` replaces it.
            log.error("password_hash_invalid", extra={"error": str(exc)})
            return False

    def needs_rehash(self, stored_hash: str) -> bool:
        try:
            return self._hasher.check_needs_rehash(stored_hash)
        except InvalidHashError:
            return False
