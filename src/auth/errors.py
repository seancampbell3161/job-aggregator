"""Auth errors. PasswordRejected's message is shown to the user as-is."""
from __future__ import annotations


class AlreadyClaimed(Exception):
    """A password already exists; only the first one can be claimed."""


class WrongPassword(Exception):
    """The current password did not verify."""


class PasswordRejected(ValueError):
    """A new password breaks the length rule. str(exc) is user-facing."""
