"""Cross-origin protection for state-changing requests.

The algorithm Go 1.25 ships as http.CrossOriginProtection. Browsers label
requests with Sec-Fetch-Site (to HTTPS and localhost origins) and always send
Origin on POST, so those headers say whether a form or fetch came from this
site. A request carrying neither doesn't come from a browser (curl,
TestClient) and can't be forged by a web page. With the SameSite=Lax session
cookie this replaces CSRF tokens — and unlike SameSite alone it also blocks
another service on this host at a different port, which counts as same-site.

On plain-HTTP LAN or Tailscale addresses browsers omit Sec-Fetch-Site, so the
Origin-vs-Host comparison decides. A plain-HTTP reverse proxy must therefore
pass the original Host header through."""
from __future__ import annotations

import logging
from typing import Mapping
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse

log = logging.getLogger(__name__)

UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def cross_origin_allowed(method: str, headers: Mapping[str, str]) -> bool:
    """Whether a request may proceed. ``headers`` is looked up by lowercase
    name (Starlette's Headers is case-insensitive)."""
    if method.upper() not in UNSAFE_METHODS:
        return True
    fetch_site = headers.get("sec-fetch-site")
    if fetch_site is not None:
        return fetch_site in ("same-origin", "none")
    origin = headers.get("origin")
    if origin is None:
        return True
    try:
        netloc = urlsplit(origin).netloc
    except ValueError:
        return False
    return bool(netloc) and netloc.lower() == headers.get("host", "").lower()


def register_cross_origin_guard(app: FastAPI) -> None:
    @app.middleware("http")
    async def _cross_origin_guard(request: Request, call_next):
        if not cross_origin_allowed(request.method, request.headers):
            log.warning("cross_origin_blocked", extra={
                "method": request.method,
                "path": request.url.path,
                "sec_fetch_site": request.headers.get("sec-fetch-site"),
                "origin": request.headers.get("origin"),
                "host": request.headers.get("host"),
            })
            return PlainTextResponse("Cross-origin request blocked.", status_code=403)
        return await call_next(request)
