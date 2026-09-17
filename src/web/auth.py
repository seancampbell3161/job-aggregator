"""The web UI login: the session cookie and the /welcome, /login, /logout,
and /account/password pages."""
from __future__ import annotations

import logging
from urllib.parse import urlsplit

from fastapi import FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from src.auth.errors import AlreadyClaimed, PasswordRejected, WrongPassword
from src.auth.passwords import MIN_PASSWORD_LENGTH
from src.auth.service import SESSION_IDLE_TTL

log = logging.getLogger(__name__)

SESSION_COOKIE = "jobagg_session"


def set_session_cookie(response: Response, request: Request, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE, token,
        max_age=int(SESSION_IDLE_TTL.total_seconds()), path="/",
        httponly=True, samesite="lax", secure=request.url.scheme == "https",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, samesite="lax")


def safe_next(value: str) -> str:
    """``value`` when it is a path on this site, else "/". Refuses
    protocol-relative URLs (//host), backslashes (browsers read /\\host as
    //host), absolute URLs, and control characters — each an open redirect."""
    if (not value.startswith("/") or value.startswith("//") or "\\" in value
            or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value)):
        return "/"
    parts = urlsplit(value)
    if parts.scheme or parts.netloc:
        return "/"
    return value


def _page(request: Request, name: str, *, status_code: int = 200,
          headers: dict[str, str] | None = None, **context) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request, name, {"min_length": MIN_PASSWORD_LENGTH, **context},
        status_code=status_code, headers=headers,
    )


def _signed_in_redirect(request: Request, url: str, token: str) -> RedirectResponse:
    response = RedirectResponse(url, status_code=303)
    set_session_cookie(response, request, token)
    return response


def _throttled(request: Request, template: str, wait: int, **context) -> HTMLResponse:
    log.warning("login_throttled", extra={"retry_after_s": wait})
    return _page(request, template, status_code=429, headers={"Retry-After": str(wait)},
                 error=f"Too many attempts. Try again in {wait} s.", **context)


def register_auth_routes(app: FastAPI) -> None:
    @app.get("/welcome", response_class=HTMLResponse)
    def welcome(request: Request):
        if request.app.state.auth.has_password():
            return RedirectResponse("/login?claimed=1", status_code=303)
        return _page(request, "welcome.html")

    @app.post("/welcome", response_class=HTMLResponse)
    def create_password(request: Request, password: str = Form(""), confirm: str = Form("")):
        auth = request.app.state.auth
        if auth.has_password():
            return RedirectResponse("/login?claimed=1", status_code=303)
        if password != confirm:
            return _page(request, "welcome.html", status_code=400,
                         error="The passwords don't match.")
        try:
            token = auth.claim(password)
        except AlreadyClaimed:
            return RedirectResponse("/login?claimed=1", status_code=303)
        except PasswordRejected as exc:
            return _page(request, "welcome.html", status_code=400, error=str(exc))
        log.info("password_created")
        return _signed_in_redirect(request, "/", token)

    @app.get("/login", response_class=HTMLResponse)
    def login(request: Request, next_path: str = Query("", alias="next"), claimed: str = ""):
        if not request.app.state.auth.has_password():
            return RedirectResponse("/welcome", status_code=303)
        return _page(request, "login.html", next=safe_next(next_path), claimed=claimed == "1")

    @app.post("/login", response_class=HTMLResponse)
    def sign_in(request: Request, password: str = Form(""),
                next_path: str = Form("", alias="next")):
        auth, throttle = request.app.state.auth, request.app.state.login_throttle
        target = safe_next(next_path)
        if not auth.has_password():
            return RedirectResponse("/welcome", status_code=303)
        wait = throttle.check()
        if wait is not None:
            return _throttled(request, "login.html", wait, next=target)
        token = auth.login(password)
        if token is None:
            failures = throttle.record_failure()
            log.warning("login_failed", extra={"consecutive_failures": failures})
            return _page(request, "login.html", status_code=401, next=target,
                         error="Incorrect password.")
        throttle.record_success()
        return _signed_in_redirect(request, target, token)

    @app.post("/logout")
    def sign_out(request: Request):
        request.app.state.auth.logout(request.cookies.get(SESSION_COOKIE))
        response = RedirectResponse("/login", status_code=303)
        clear_session_cookie(response)
        return response

    @app.get("/account/password", response_class=HTMLResponse)
    def account_password(request: Request):
        return _page(request, "account_password.html")

    @app.post("/account/password", response_class=HTMLResponse)
    def change_password(request: Request, current: str = Form(""),
                        password: str = Form(""), confirm: str = Form("")):
        auth, throttle = request.app.state.auth, request.app.state.login_throttle
        wait = throttle.check()
        if wait is not None:
            return _throttled(request, "account_password.html", wait)
        if password != confirm:
            return _page(request, "account_password.html", status_code=400,
                         error="The new passwords don't match.")
        try:
            token = auth.change_password(current, password)
        except WrongPassword:
            failures = throttle.record_failure()
            log.warning("login_failed", extra={"consecutive_failures": failures})
            return _page(request, "account_password.html", status_code=400,
                         error="Incorrect password.")
        except PasswordRejected as exc:
            return _page(request, "account_password.html", status_code=400, error=str(exc))
        throttle.record_success()
        response = _page(request, "account_password.html", changed=True)
        set_session_cookie(response, request, token)
        return response
