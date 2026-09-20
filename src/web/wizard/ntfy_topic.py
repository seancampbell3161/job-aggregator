"""Suggest an ntfy topic and render its QR.

On ntfy.sh a topic name IS the credential: anyone who knows it can subscribe
and read every alert. So a suggested topic is generated from
secrets.token_urlsafe, not from the user's name or the app's — 20 URL-safe
bytes, lowercased to the character set ntfy topics accept."""
from __future__ import annotations

import io
import re
import secrets

NTFY_BASE = "https://ntfy.sh/"


def suggest_topic() -> str:
    raw = secrets.token_urlsafe(20).lower()
    slug = re.sub(r"[^a-z0-9]+", "-", raw).strip("-")
    return f"{NTFY_BASE}job-alerts-{slug[:24]}"


def topic_qr_svg(url: str) -> str:
    """An inline SVG, so the page never fetches a QR from a third party (which
    would hand the topic — the credential — to whoever served the image).

    segno's SVG writer only accepts a file-like object opened for *bytes*
    (its docstring says so explicitly: "a file-like object supporting to
    write bytes") — handing it a StringIO raises ``TypeError: string
    argument expected, got 'bytes'``. Write to a BytesIO and decode."""
    import segno

    buf = io.BytesIO()
    segno.make(url, error="m").save(buf, kind="svg", scale=7, border=2)
    return buf.getvalue().decode("utf-8")
