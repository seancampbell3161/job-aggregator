"""HMAC signed tokens for the hosted tailor endpoint.

Token = "<exp>.<sig>" where sig = urlsafe-b64(HMAC-SHA256(secret, "<job_id>|<exp>")).
The signature is the gate (unforgeable without the secret); exp bounds a leaked
link's lifetime. Rotate the SSM secret to invalidate every outstanding link."""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from urllib.parse import quote


def _sig(job_id: str, exp: int, secret: str) -> str:
    mac = hmac.new(secret.encode(), f"{job_id}|{exp}".encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).decode().rstrip("=")


def sign_token(job_id: str, exp: int, secret: str) -> str:
    return f"{exp}.{_sig(job_id, exp, secret)}"


def verify_token(token: str, job_id: str, secret: str, *, now: int | None = None) -> bool:
    now = int(time.time()) if now is None else now
    try:
        exp_str, sig = (token or "").split(".", 1)
        exp = int(exp_str)
    except (ValueError, AttributeError):
        return False
    if now > exp:
        return False
    return hmac.compare_digest(sig, _sig(job_id, exp, secret))


def build_tailor_url(job_id: str, *, endpoint_url: str, secret: str,
                     ttl_days: int = 30, now: int | None = None) -> str:
    """The signed deep-link the alert carries: endpoint + ?job_id + a token that
    expires in ttl_days. quote(safe='') percent-encodes the ':' in job ids."""
    now = int(time.time()) if now is None else now
    exp = now + ttl_days * 86400
    return f"{endpoint_url}?job_id={quote(job_id, safe='')}&t={sign_token(job_id, exp, secret)}"
