"""Password hashing (argon2id) and the new-password length rule."""
import logging

import pytest
from argon2 import PasswordHasher

from src.auth.errors import PasswordRejected
from src.auth.passwords import (
    MAX_PASSWORD_LENGTH,
    MIN_PASSWORD_LENGTH,
    Passwords,
    validate_new_password,
)
from tests.auth_helpers import cheap_hasher


def test_default_hasher_is_argon2id():
    stored = Passwords().hash("correct horse")
    assert stored.startswith("$argon2id$")
    assert Passwords().verify(stored, "correct horse") is True


def test_verify_round_trip_and_mismatch():
    passwords = Passwords(cheap_hasher())
    stored = passwords.hash("correct horse")
    assert "correct horse" not in stored
    assert passwords.verify(stored, "correct horse") is True
    assert passwords.verify(stored, "wrong horse") is False


def test_malformed_stored_hash_verifies_false_and_logs(caplog):
    with caplog.at_level(logging.ERROR, logger="src.auth.passwords"):
        assert Passwords(cheap_hasher()).verify("not-an-argon2-hash", "anything") is False
    assert [r.message for r in caplog.records] == ["password_hash_invalid"]


def test_needs_rehash_after_a_parameter_change():
    old = Passwords(cheap_hasher())
    stored = old.hash("correct horse")
    assert old.needs_rehash(stored) is False
    stronger = Passwords(PasswordHasher(time_cost=2, memory_cost=8, parallelism=1))
    assert stronger.needs_rehash(stored) is True
    assert stronger.verify(stored, "correct horse") is True


def test_needs_rehash_is_false_for_a_malformed_hash():
    assert Passwords(cheap_hasher()).needs_rehash("not-an-argon2-hash") is False


def test_limits():
    assert (MIN_PASSWORD_LENGTH, MAX_PASSWORD_LENGTH) == (8, 1024)


@pytest.mark.parametrize("password", ["a" * 8, "a" * 1024, "é" * 8, " " * 8])
def test_valid_lengths(password):
    validate_new_password(password)


@pytest.mark.parametrize("password, message", [
    ("", "Use at least 8 characters."),
    ("a" * 7, "Use at least 8 characters."),
    ("é" * 7, "Use at least 8 characters."),
    ("a" * 1025, "Use at most 1024 characters."),
])
def test_invalid_lengths(password, message):
    with pytest.raises(PasswordRejected) as exc:
        validate_new_password(password)
    assert str(exc.value) == message


def test_password_rejected_is_a_value_error():
    assert issubclass(PasswordRejected, ValueError)
