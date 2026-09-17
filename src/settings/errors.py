"""Exceptions raised by the settings store and service."""
from __future__ import annotations


class SettingsInvalid(Exception):
    """A settings document or document body failed validation; nothing was
    written. ``errors`` is a list of {"loc": str, "msg": str} where loc is a
    dotted settings path, a document kind, or "" for document-level problems."""

    def __init__(self, errors: list[dict]) -> None:
        self.errors = errors
        super().__init__("; ".join(
            f"{e['loc']}: {e['msg']}" if e.get("loc") else e["msg"] for e in errors
        ))


class StaleWrite(Exception):
    """The caller's base version/document id is no longer the latest — another
    writer saved in between. Re-read and retry."""


class NotConfigured(Exception):
    """The operation needs an existing settings version (run `import` first)."""
